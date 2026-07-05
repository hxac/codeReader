# no_std / loom / 可移植性抽象层

## 1. 本讲目标

学完本讲后，读者应当能够：

1. 说清楚 crossbeam-epoch 同时面对的 **四个移植维度**：`no_std`（不依赖标准库）、`loom`（并发模型检验器）、`miri`（未定义行为检测器）、以及 `target_has_atomic`（平台原子能力）。
2. 读懂 `src/lib.rs` 顶部的 `primitive` 抽象层，理解它如何让上层代码「不知道自己跑在 loom 还是真实硬件上」。
3. 手写一个对齐 loom 风格 API 的 `UnsafeCell` 包装，理解 `with` / `with_mut` 的设计动机。
4. 解释 `const_fn!` 宏如何用条件编译让同一个函数签名在 `const` 与非 `const` 之间切换。
5. 解释 `alloc_helper::Global` 为何存在，以及 `fetch_and` / `fetch_or` / `fetch_xor` 为什么在 `miri` 下走 `AtomicPtr::fetch_*`、在非 `miri` 下走 `AtomicUsize::fetch_*`——以及这一切与 **strict provenance（严格来源）** 和 CHERI 的关系。

本讲是「专家层」的可移植性专题，**不再讲 EBR 的回收逻辑**，而是讲「同一份算法源码如何同时跑在多种编译目标上而不分叉」。前置认知来自 u1-l2（特性开关与构建）和 u4-l14（默认收集器与线程局部存储）。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立几个直觉。

### 2.1 为什么需要抽象层：同一份代码，多个「世界」

crossbeam-epoch 的核心算法（pin、epoch 推进、垃圾回收）一旦写错，就是极难复现的数据竞争。为了把算法验证到位，作者希望同一份源码能跑在**至少三种环境**里：

| 环境 | 作用 | 它提供的「原子操作」是什么 |
|------|------|--------------------------|
| 真实硬件（std / no_std+alloc） | 生产运行 | `core::sync::atomic::*`、`alloc` 分配器 |
| [loom](https://github.com/tokio-rs/loom) | 并发模型检验（穷举线程交错） | `loom::sync::atomic::*`，会记录每一次访问以便回放 |
| [Miri](https://github.com/rust-lang/miri) | 未定义行为检测（含 Stacked Borrows / provenance） | 真实原子，但对指针来源（provenance）极严格 |

问题是：这三套环境的 API **不完全一致**。例如 `loom::cell::UnsafeCell` 的方法叫 `with` / `with_mut`，而 `core::cell::UnsafeCell` 只有一个 `get()`。如果上层代码直接调 `core::cell::UnsafeCell::get`，那在 loom 下就无法编译；如果直接调 `loom` 的 `with_mut`，那在真实环境下也无法编译。

解决之道就是本讲的灵魂：**在 `src/lib.rs` 顶部建一个 `primitive` 模块，用 `cfg` 把「真实实现」和「loom 实现」分别 `use` 进相同的名字空间，上层代码只引用 `crate::primitive::...` 这个统一入口。**

### 2.2 术语速查

- **no_std**：crate 不链接 `std`，只用 `core`（与可选的 `alloc`）。crossbeam-epoch 顶部就写着 `#![no_std]`。
- **loom**：一个把多线程执行「可能的所有交错」做有界状态穷举的工具，用于在测试期发现数据竞争。
- **Miri**：Rust 官方的 UB 检测器，运行在 MIR 层，能检查内存越界、无效对齐、use-after-free、**provenance 违规**等。
- **provenance（指针来源）**：Rust 内存模型里，一个裸指针不只携带「地址整数」，还携带「它从哪个分配来」的元数据。把指针强转成整数再转回来会**丢失 provenance**，在严格模型下解引用是 UB。
- **strict provenance**：Rust 1.84 起逐步推进的、要求指针操作保留 provenance 的编程风格，目的是兼容 CHERI 等带能力（capability）的硬件。
- **CHERI**：一类把指针扩展为「能力指针」（含权限与来源标记，宽度 128 位）的硬件架构；在那里 `addr as *mut T` 这种「整数即指针」的写法会失效。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `crossbeam-epoch/` 下）：

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs) | crate 根：`#![no_std]`、`primitive` 抽象层、`const_fn!` 宏、子模块装配 |
| [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs) | no_std 下的自定义分配器 `Global` 与 `without_provenance_mut` |
| [src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | `Atomic`/`Owned`/`Shared`/`Pointable`，含 `fetch_and/or/xor` 的双实现与 `const_fn!` 用法 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml) | 特性开关（`std`/`alloc`/`loom`）与 `crossbeam_loom` 依赖的来源 |

永久链接基准（本讲所有链接均基于此 HEAD）：

```
https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/
```

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：

1. **primitive 抽象层**：loom 分支 vs std/core 分支（含 `cell` / `sync` / `thread_local`）。
2. **`const_fn!` 宏**：在 `const` 与非 `const` 之间切换。
3. **`alloc_helper::Global`**：no_std 下的自定义分配器。
4. **strict provenance**：`without_provenance_mut` 与 `fetch_and/or/xor` 的 miri / 非 miri 分叉。

### 4.1 primitive 抽象层：在 loom 与真实环境之间切换

#### 4.1.1 概念说明

`primitive` 模块要解决的问题可以用一句话概括：**让上层算法源码对「自己跑在 loom 还是真实环境」完全无感。**

为什么这是难题？因为 loom 为了能回放每一次内存访问，**故意**重写了一套与标准库同名但语义更严格的类型：

- `loom::sync::atomic::AtomicUsize`：会记录访问历史。
- `loom::cell::UnsafeCell`：强制你用 `with` / `with_mut` 闭包形式访问，从而让 loom 知道这次访问是只读还是可写——这是它建模数据竞争的依据。
- `loom::sync::Arc`：loom 版本的引用计数。

如果上层代码直接写 `core::cell::UnsafeCell`，loom 就无法追踪；如果直接写 `loom::cell::UnsafeCell`，在真实硬件上又编不过（loom 不是生产依赖）。唯一干净的解法是：**在 crate 根按 `cfg` 把两套实现 `use` 到同一组名字下**，上层代码只写 `crate::primitive::cell::UnsafeCell`。这样：

- 真实环境编译时，这名字解析到「标准库 + 一层薄包装」。
- loom 编译时，这名字解析到 `loom::cell::UnsafeCell`。
- 上层算法源码**一个字都不用改**。

这正是 loom 官方文档推荐的 [Handling Loom API differences](https://github.com/tokio-rs/loom#handling-loom-api-differences) 做法。

#### 4.1.2 核心流程

`primitive` 模块的整体结构是一个**两选一的 `cfg` 分支**：

```
src/lib.rs
├── #[cfg(crossbeam_loom)]          mod primitive { … 全是 loom:: 的 re-export … }
└── #[cfg(not(crossbeam_loom))]     mod primitive { … 标准库 + UnsafeCell 包装 … }
```

三个子名字空间在两条分支里**逐一对齐**：

| 名字空间 | loom 分支提供 | 真实分支提供 |
|---------|--------------|-------------|
| `primitive::cell::UnsafeCell` | `loom::cell::UnsafeCell`（原生 `with`/`with_mut`） | 自定义包装，包住 `core::cell::UnsafeCell` 并补出 `with`/`with_mut` |
| `primitive::sync::atomic::*` | `loom::sync::atomic::{AtomicU64, AtomicPtr, AtomicUsize, Ordering, fence}` | `core::sync::atomic` 整体 re-export |
| `primitive::sync::Arc` | `loom::sync::Arc` | `alloc::sync::Arc`（仅在 `alloc` 下） |
| `primitive::thread_local` | `loom::thread_local` | `std::thread_local`（仅在 `std` 下） |

关键在于：**两条分支对外暴露的名字完全一致**，因此上层（`atomic.rs`、`internal.rs` 等）只需 `use crate::primitive::...` 即可，编译器在编译期替你做了切换。

#### 4.1.3 源码精读

先看 crate 顶部的两行「环境声明」。[src/lib.rs:51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51) 宣告 `#![no_std]`；接着按 `cfg` 把 loom 别名进来：

[src/lib.rs:64-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L64-L65) —— 当启用了 `crossbeam_loom` cfg 时，把外部 crate `loom_crate` 引入为 `loom`。`loom_crate` 这个依赖本身只在 `cfg(crossbeam_loom)` 下才被拉入（见 [Cargo.toml:52-L53](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L52-L53)），而 `crossbeam_loom` cfg 是由 `crossbeam-utils/loom` 透传设置的。

**loom 分支**（[src/lib.rs:69-L91](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L69-L91)）：几乎全是 `use` 转发。注意三处细节：

- `cell::UnsafeCell` 直接转发 `loom::cell::UnsafeCell`（loom 原生就有 `with`/`with_mut`，无需包装）。
- `sync::atomic` 里 `AtomicU64` 还多了一层 `#[cfg(target_has_atomic = "64")]`（[src/lib.rs:77-L78](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L77-L78)），因为不是所有平台都有 64 位原子（见 u5-l17 对 `AtomicEpoch` 的平台取舍）。
- [src/lib.rs:81-L86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L81-L86) 是一处「无奈的妥协」注释：loom 目前不支持 `compiler_fence`（[tokio-rs/loom#117](https://github.com/tokio-rs/loom/issues/117)），于是用更强的 `fence` 顶替。代价是 loom 可能漏报某些竞争（fence 比 compiler_fence 强），但这是目前能做到的最好。

**真实分支**（[src/lib.rs:92-L131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L92-L131)）：与 loom 分支逐项对齐，但来源换成标准库。最值得注意的是 `cell::UnsafeCell` 不是直接转发，而是**手写了一层包装**：

[src/lib.rs:96-L121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L96-L121) —— `#[repr(transparent)]` 的新类型 `UnsafeCell<T>(::core::cell::UnsafeCell<T>)`，并补出三个方法：

- `new`（[src/lib.rs:108-L110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L108-L110)）：`const fn`，包一层。
- `with`（[src/lib.rs:113-L115](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L113-L115)）：`f(self.0.get())`，把内部裸指针交给闭包。
- `with_mut`（[src/lib.rs:118-L120](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L118-L120)）：同上但 `*mut T`。

源码里紧贴的一长段注释（[src/lib.rs:101-L105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L101-L105)）点明了动机：loom 的 `UnsafeCell` API 与标准库不同，为了让上层代码对二者无感，就用这个包装把标准库 `UnsafeCell`「伪装」成 loom 的 API。这正是 4.1.4 的实践对象。

`sync` 与 `thread_local` 的真实分支则更直接：`sync::atomic` 整体转发 `core::sync::atomic`（[src/lib.rs:126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L126)）；`Arc` 在 `alloc` 下转发 `alloc::sync::Arc`（[src/lib.rs:124-L125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L124-L125)）；`thread_local` 只在 `std` 下转发 `std::thread_local`（[src/lib.rs:129-L130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L129-L130)）——这呼应了 u4-l14 讲过的「默认收集器只在 `std` 下可用」。

#### 4.1.4 代码实践：手写对齐 loom 风格的 UnsafeCell 包装

**实践目标**：亲手实现一遍 `primitive::cell::UnsafeCell` 的真实分支包装，体会「为什么 loom 要用闭包、为什么标准库只有 `get()`」。

**操作步骤**（在自己的实验 crate 里，不依赖 crossbeam-epoch）：

1. 新建一个 `lib` crate（`cargo new --lib portable_cell && cd portable_cell`）。
2. 在 `src/lib.rs` 写入下面的「示例代码」（注意标注）：

```rust
// 示例代码：模拟 crossbeam-epoch 的 primitive::cell::UnsafeCell 包装
use core::cell::UnsafeCell as StdUnsafeCell;

#[repr(transparent)]
pub struct UnsafeCell<T>(StdUnsafeCell<T>);

impl<T> UnsafeCell<T> {
    #[inline]
    pub const fn new(data: T) -> Self {
        Self(StdUnsafeCell::new(data))
    }

    /// 只读访问：把内部 `*const T` 交给闭包。
    #[inline]
    pub fn with<R>(&self, f: impl FnOnce(*const T) -> R) -> R {
        f(self.0.get())
    }

    /// 可写访问：把内部 `*mut T` 交给闭包。
    #[inline]
    pub fn with_mut<R>(&self, f: impl FnOnce(*mut T) -> R) -> R {
        f(self.0.get())
    }
}
```

3. 写一个最小用例验证它能工作：

```rust
// 示例代码
let cell = UnsafeCell::new(42u32);
let v = cell.with(|p| unsafe { *p });           // 只读
cell.with_mut(|p| unsafe { *p = 7 });            // 可写
assert_eq!(cell.with(|p| unsafe { *p }), 7);
```

**需要观察的现象**：

- 用 `cargo build` 编译通过；如果你的实验 crate 是 `no_std` 的，`cargo build --no-default-features` 也应通过，因为只用到了 `core`。
- 尝试把 `with` 的闭包参数改成 `&T` 而不是 `*const T`：你会发现自己不得不在 `with` 内部就写下 `unsafe { &*self.0.get() }`，从而把 `unsafe` 从「调用方」挪到了「封装方」。crossbeam 选择传裸指针，是为了把 `unsafe` 责任显式留给调用方（与 loom 的语义一致：loom 需要调用方声明读还是写）。

**预期结果**：编译通过、断言成立。

**待本地验证**：若你的工具链装了 loom，可把上面的 `StdUnsafeCell` 换成 `loom::cell::UnsafeCell` 并保持 `with`/`with_mut` 调用不变，验证「上层代码无需改动」这一核心承诺。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `primitive::cell::UnsafeCell` 的真实分支要 `#[repr(transparent)]`？
**答案**：因为 `#[repr(transparent)]` 保证新类型 `UnsafeCell<T>` 与内部的 `core::cell::UnsafeCell<T>` 在内存布局上完全相同（单字段零开销），这样跨 `cfg` 分支时，无论解析到哪一边，ABI 与布局都一致，不会因为包装层引入额外开销或布局差异。

**练习 2**：loom 分支里 `compiler_fence` 用 `fence` 顶替，为什么说「可能漏报某些竞争」？
**答案**：`compiler_fence` 只约束编译器重排（不发射 CPU 屏障指令），而 `fence` 是真正的全屏障。loom 用更强的 `fence` 顶替 `compiler_fence`，意味着它会把本不该被「全局同步」的操作也当成需要同步，从而**过度约束**线程交错——某些在真实弱内存模型下可能发生、但 loom 模型里被更强的 fence 排除掉的交错就不会被探查到，于是可能漏报。

**练习 3**：`thread_local` 在两条分支里分别来自哪里？为什么 `no_std` 下没有？
**答案**：loom 分支来自 `loom::thread_local`，真实分支来自 `std::thread_local`（仅 `feature = "std"`）。`core`/`alloc` 不提供线程局部存储原语，故 `no_std` 下无法提供默认收集器（这正是 u4-l14 强调「`pin()` 仅 `std` 可用」的原因）。

---

### 4.2 `const_fn!` 宏：在 `const` 与非 `const` 之间切换

#### 4.2.1 概念说明

`primitive` 抽象层解决的是「类型 API 不同」，`const_fn!` 宏解决的是「**同一个函数能不能在常量上下文求值**」的差异。

具体场景：`Atomic::<T>::null()` 想做成 `const fn`，这样 `static A: Atomic<u64> = Atomic::null();` 这类静态初始化才能成立（这在写无锁全局表时极常见）。问题在于——

- 真实环境下，`core::sync::atomic::AtomicPtr::new` 是 `const fn`，所以 `null()` 可以是 `const`。
- loom 环境下，`loom::sync::atomic::AtomicPtr::new` **不是** `const fn`（它需要登记到 loom 的内部状态），所以 `null()` 不能是 `const`。

你无法在一个签名里写「有时候 const、有时候不 const」。删掉 `const` 会牺牲真实环境的静态初始化能力；硬写 `const` 又在 loom 下编不过。`const_fn!` 宏就是用条件编译把这两种写法**从同一份源码里分别展开**。

#### 4.2.2 核心流程

宏的思路是：**用一对 `cfg` / `cfg(not(...))` 把同一段函数体展开两次，一次带 `const`、一次不带。** 调用方用一个 `const_if:` 参数指明「在哪个条件下保留 `const`」。

伪代码：

```
const_fn! {
    const_if: #[cfg(NOT_LOOM)];   // 意思是：当 NOT_LOOM 成立时保留 const
    pub const fn null() -> Self { … }   // 注意这里写成了 const
}
```

展开后等价于：

```
#[cfg(NOT_LOOM)]     pub const fn null() -> Self { … }   // 真实环境：保留 const
#[cfg(not(NOT_LOOM))] pub      fn null() -> Self { … }   // loom 环境：去掉 const
```

两条 `cfg` 互斥，所以实际只有一条被编译器选中，但源码只需写一遍。

#### 4.2.3 源码精读

宏定义在 [src/lib.rs:136-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L136-L151)：

```rust
macro_rules! const_fn {
    (
        const_if: #[cfg($($cfg:tt)+)];
        $(#[$($attr:tt)*])*
        $vis:vis const $($rest:tt)*
    ) => {
        #[cfg($($cfg)+)]
        $(#[$($attr)*])*
        $vis const $($rest)*
        #[cfg(not($($cfg)+))]
        $(#[$($attr)*])*
        $vis $($rest)*
    };
}
```

要点：

- 模式里要求源码写成 `$vis:vis const $($rest:tt)*`，即调用方**必须**写 `const`。宏在展开时把 `const` 单独「扣」出来：第一条分支原样保留 `$vis const $($rest)`，第二条分支只输出 `$vis $($rest)`（吃掉了 `const`）。
- `const_if: #[cfg($($cfg:tt)+)]` 用 `tt`（token tree）贪婪捕获整段 cfg 条件，允许是 `not(crossbeam_loom)` 这种带括号的形式。
- `$(#[$($attr:tt)*])*` 把文档注释等属性在两条分支里都复制一份，避免漏掉。

典型调用是 `Atomic::null()`：

[src/atomic.rs:321-L338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L321-L338) —— `const_if: #[cfg(not(crossbeam_loom))]`，配合 `pub const fn null()`。展开后：

- 非 loom（生产）：`pub const fn null() -> Self { … }`，于是 `AtomicPtr::new(ptr::null_mut())` 用的是 `core`/`alloc` 的 const 版 `AtomicPtr::new`，可静态初始化。
- loom（测试）：`pub fn null() -> Self { … }`，`AtomicPtr::new` 走 loom 版本，**不可**静态初始化，但能被 loom 追踪。

> 注：同样的宏手法也用在 `Owned::init`（[src/atomic.rs:1046](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1046) 附近）等需要 const 的构造点上，原理一致。

#### 4.2.4 代码实践：用 `cargo expand` 看宏展开

**实践目标**：亲眼确认 `const_fn!` 在 loom 与非 loom 下分别展开成 `const` 与非 `const`。

**操作步骤**：

1. 安装展开工具：`cargo install cargo-expand`（可选；若没有，则改为阅读理解）。
2. 在 crossbeam-epoch 目录下分别执行：

   ```bash
   cargo expand --lib atomic 2>/dev/null | grep -A3 'fn null'
   cargo expand --features loom --lib atomic 2>/dev/null | grep -A3 'fn null'
   ```

   > 第一条展开真实分支，第二条展开 loom 分支。

**需要观察的现象**：

- 第一条输出里 `null` 前面应有 `const`。
- 第二条输出里 `null` 前面应**没有** `const`。

**预期结果**：两条命令的 `fn null` 签名一个带 `const`、一个不带，恰好对应宏的两条分支。

**待本地验证**：`--features loom` 需要 loom 工具链可用；若环境无法安装，请改用「源码阅读」方式——直接对照 [src/lib.rs:144-L149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L144-L149) 手工推演展开结果。

#### 4.2.5 小练习与答案

**练习 1**：如果把宏模式里的 `$vis:vis const $($rest:tt)*` 改成 `$vis:vis $($rest:tt)*`（去掉 `const`），会怎样？
**答案**：宏就失去了「扣出 const 再选择性丢弃」的能力——它无法再区分两条分支，也就没法在 loom 分支去掉 `const`。这个 `const` 既是源码里的书写要求，也是宏用来定位「const 关键字位置」的锚点。

**练习 2**：为什么不直接写两个 `cfg` 函数（`#[cfg(not(loom))] pub const fn null()` 和 `#[cfg(loom)] pub fn null()`）而要用宏？
**答案**：可以那么写，但要重复两遍函数体，维护时容易两边不同步。宏把函数体只写一遍、自动在两条 `cfg` 下各复制一份（一份带 const、一份不带），是「DRY」与「条件编译」的结合。代价是宏可读性略差。

**练习 3**：`null()` 在 loom 下不能 `const`，这是否意味着 loom 测试里不能有 `static` 原子？
**答案**：是的，loom 下你不能用 `static A: Atomic = Atomic::null()` 这种常量初始化；通常 loom 测试会把原子放进一个 `lazy_static!` / 函数局部变量里，由 loom 在每次模型执行时重新构造。这是 loom 建模的固有约束。

---

### 4.3 `alloc_helper::Global`：no_std 下的自定义分配器

#### 4.3.1 概念说明

crossbeam-epoch 要在堆上分配对象（`Owned::new`、`Owned::<[MaybeUninit<T>]>::init` 等），但它是 `#![no_std]` 的。`alloc` crate 提供了 `alloc::alloc::alloc` / `dealloc` / `handle_alloc_error` 这些底层原语，但没有提供稳定的 `alloc::alloc::Global`（那个还在 nightly）。于是 crossbeam 自己写了一个最小的 `Global` 分配器封装，放在 [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs)。

它做三件事：

1. **`allocate(layout)` / `allocate_zeroed(layout)`**：按布局分配，返回 `Option<NonNull<u8>>`。
2. **`deallocate(ptr, layout)`**：按布局释放。
3. **正确处理零大小类型（ZST）**：`Layout` 大小为 0 时返回一个对齐良好的悬垂指针（`dangling`），且释放时跳过真正 `dealloc`。

其中第 3 点会牵出一个深坑——`dangling` 怎么构造？这就引出了下一节的 `without_provenance_mut`。

#### 4.3.2 核心流程

`Global::alloc_impl` 的判定流程：

```
alloc_impl(layout, zeroed):
  if layout.size() == 0:
      return dangling(layout)        # ZST：返回对齐良好的「空」指针
  else:
      raw = zeroed ? alloc_zeroed(layout) : alloc(layout)
      return NonNull::new(raw)       # 分配失败返回 None
```

`dangling(layout)` 用 `without_provenance_mut::<u8>(layout.align())` 造一个「地址 = 对齐值、无 provenance」的指针。`deallocate` 则反向：`size == 0` 时直接返回（与 `dangling` 对称，不真正释放），否则 `dealloc`。

#### 4.3.3 源码精读

`Global` 结构本身是个空标记类型（[src/alloc_helper.rs:7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L7)），所有方法以 `&self` 调用（注释 `#[allow(clippy::unused_self)]` 表明它其实不读 `self`，保留 `&self` 是为了对齐未来稳定的 `Allocator` trait 形态）。

核心分配逻辑 [src/alloc_helper.rs:12-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L12-L34)：

- ZST 分支（[src/alloc_helper.rs:21-L22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L21-L22)）：大小为 0 时调 `dangling(layout)`。
- `dangling` 辅助（[src/alloc_helper.rs:14-L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L14-L19)）：因为稳定的 `Layout::dangling` 还没提供，就用 `without_provenance_mut::<u8>(layout.align())` 自己造——把「对齐值」当作地址，造一个无 provenance 的指针。这在语义上等价于「一个永不与任何真实分配重叠、且满足对齐要求的哨兵指针」。
- 真正分配分支（[src/alloc_helper.rs:23-L32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L23-L32)）：按 `zeroed` 选 `alloc_zeroed` 或 `alloc`，再 `NonNull::new` 把 `*mut u8` 升级为 `Option<NonNull<u8>>`（分配失败为 `None`）。

三个公开方法 `allocate` / `allocate_zeroed` / `deallocate`（[src/alloc_helper.rs:38-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L38-L65)）都带 `#[cfg_attr(miri, track_caller)]`——这能在 Miri 报错时打印调用栈位置，便于定位（即使没有 panic，对 Miri 回溯也有帮助）。

`Global` 真正被消费的地方是 `Pointable for [MaybeUninit<T>]`（动态数组布局，详见 u2-l4）。这里只看它的分配/释放对称性：

- 分配：[src/atomic.rs:225-L235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L225-L235) 调 `Global.allocate(layout)`，失败时 `alloc::alloc::handle_alloc_error(layout)`（按惯例 abort 而非返回 `Result`）。
- 释放：[src/atomic.rs:256-L262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L256-L262) 调 `Global.deallocate(NonNull::new_unchecked(...), layout)`。

注意 `as_ptr` 里那行注释 [src/atomic.rs:240](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L240)：「Use addr_of_mut for stacked borrows」——这是另一处与 Miri（Stacked Borrows）相关的可移植性细节：用 `addr_of_mut!` 而不是 `&mut (*ptr).elements` 来取字段地址，避免在 Miri 下触发借用栈违规。

#### 4.3.4 代码实践：追踪 Array 的分配—释放对称性

**实践目标**：理解 `Global` 如何被 `Pointable` 消费，以及 ZST 路径的安全性。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/atomic.rs:202-L217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L202-L217)，画出 `Array<T>` 的 `repr(C)` 布局：先是 `len: usize`，再用 `Layout::extend` 把 `Layout::array::<MaybeUninit<T>>(len)` 追加上去，最后 `pad_to_align`。
2. 对照 [src/atomic.rs:225-L235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L225-L235)（`init` 调 `Global.allocate`）与 [src/atomic.rs:256-L262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L256-L262)（`drop` 调 `Global.deallocate`），确认二者用的是**同一个 `layout`**（都来自 `Array::<T>::layout(len)`）。这是「分配与释放布局必须一致」这一 `Allocator` 契约的体现。
3. 思考：若 `len = 0`，`Array::<T>::layout(0)` 的 `size()` 是否为 0？若为 0，`Global.allocate` 走 ZST 分支返回 `dangling`，`Global.deallocate` 也因 `size == 0` 跳过 `dealloc`——两边对称，不会去释放一个「从未真正分配」的指针。

**需要观察的现象**：分配与释放的 `layout` 完全一致；ZST 路径在 `allocate` 与 `deallocate` 两侧都被 `size == 0` 短路。

**预期结果**：能用自己的话解释「为什么 `Global` 对 ZST 安全」——因为 `dangling` 返回的不是任何真实分配的地址，`deallocate` 对 `size == 0` 又是 no-op，永远不会把哨兵指针误当真实分配去 `dealloc`。

**待本地验证**：可在实验 crate 里 `Owned::<[MaybeUninit<i32>]>::init(0)`，在 Miri 下运行，观察无 UB（需 Miri 工具链）。

#### 4.3.5 小练习与答案

**练习 1**：`Global` 为什么不直接实现标准的 `Allocator` trait？
**答案**：`Allocator` trait 仍为 unstable（nightly only），crossbeam-epoch 要保持 stable 兼容（MSRV 1.74，见 [Cargo.toml:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L10)），故只能写一个 crate 内私有的等价封装。注释 [src/alloc_helper.rs:3-L6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L3-L6) 也说明它「Based on unstable alloc::alloc::Global」，且故意返回 `NonNull<u8>` 而非 `NonNull<[u8]>`。

**练习 2**：`#[cfg_attr(miri, track_caller)]` 加在 `alloc_impl` 上有什么用？
**答案**：`track_caller` 会把「调用者位置」带到函数内。即使没有 panic，Miri 报告 UB 时也能利用它打印出是哪一行触发了分配相关的违规，大幅缩短排查路径。它只在 `miri` cfg 下启用，避免影响生产编译。

**练习 3**：`handle_alloc_error` 在 `init` 失败时被调用，它的行为是什么？
**答案**：`alloc::alloc::handle_alloc_error` 按 Rust 约定会**终止进程（abort）**而非返回，因此 `Pointable::init` 在分配失败时不返回错误——这与「构造即成功」的 `Owned::new` 语义一致，把 OOM 当作不可恢复错误处理。

---

### 4.4 strict provenance：`without_provenance_mut` 与 `fetch_and/or/xor` 的双实现

#### 4.4.1 概念说明

这是本讲最微妙的一节，也是把 `alloc_helper.rs` 与 `atomic.rs` 串起来的主线：**指针 provenance（来源）**。

在 Rust 的内存模型里，裸指针 = 地址 + provenance。把指针 cast 成整数会丢 provenance；把整数 cast 回指针得到的指针「没有来源」。在 **permissive provenance**（宽松模型）下，只要地址对得上、解引用就是合法的；但在 **strict provenance**（严格模型，也是 Miri 的 Stacked Borrows / Tree Borrows、以及 CHERI 硬件所要求的）下，**丢失了 provenance 的指针解引用是 UB**。

crossbeam-epoch 在两处与这条线剧烈摩擦：

1. `without_provenance_mut`（[src/alloc_helper.rs:68-L85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L68-L85)）：**故意**要造一个「无 provenance」的指针（给 ZST 当 `dangling` 用）。它在 miri 与非 miri 下用**不同**的写法。
2. `fetch_and` / `fetch_or` / `fetch_xor`（[src/atomic.rs:651-L744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L744)）：对带 tag 的指针做位运算 RMW。它在 miri 下用 `AtomicPtr::fetch_*`、非 miri 下把 `AtomicPtr` 强转成 `AtomicUsize` 再 `fetch_*`。

理解这两处的「双实现」是本节目标。

#### 4.4.2 核心流程

**`without_provenance_mut` 的两条路径**（[src/alloc_helper.rs:76-L84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L76-L84)）：

```
#[cfg(miri)]       unsafe { core::mem::transmute(addr) }   # 整数 → 指针，明确丢 provenance
#[cfg(not(miri))]  addr as *mut T                          # 普通整数转指针
```

为什么 miri 下要用 `transmute`？因为 Miri 会区分「这个指针是从整数 transmute 来的（明确无 provenance）」与「这是普通 cast」。对于 ZST 的 `dangling` 哨兵，我们**就是要**一个无 provenance 的指针，`transmute` 在 Miri 下表达了这一意图且不会误报。而非 miri（真实硬件、CHERI 之外）下，`addr as *mut T` 足够；注释 [src/alloc_helper.rs:80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L80) 还特别指出：在 CHERI 上 `transmute` 会失效（能力指针 128 位，与 usize 位宽不匹配），所以非 miri 用 `as` 转换更稳。

**`fetch_and/or/xor` 的两条路径**（以 `fetch_and` 为例，[src/atomic.rs:651-L668](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L668)）：

```
#[cfg(miri)]       self.data.fetch_and(val, order)                              # AtomicPtr::fetch_and（保 provenance）
#[cfg(not(miri))]  (*(&self.data as *const _ as *const AtomicUsize)).fetch_and(val, order) as *mut ()
                                                                                       # 强转 AtomicUsize::fetch_and
```

源码注释（[src/atomic.rs:653-L656](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L653-L656)）解释了取舍：「理想情况下应始终用 `AtomicPtr::fetch_*`，因为它是 strict-provenance 兼容的，但它要求 Rust 1.91；所以现在只在 `cfg(miri)` 下用。基于 `AtomicUsize` cast 的写法仍是 permissive-provenance 兼容且 sound 的。」这段话把整个 4.4 节的动机说透了——**miri 要 strict provenance，故用 `AtomicPtr`；生产要兼容 MSRV 1.74，故退回 `AtomicUsize` cast。**

#### 4.4.3 源码精读

先看 `fetch_and` 的预处理：[src/atomic.rs:652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L652) `let val = val | !low_bits::<T>();`。这是 u2-l8 讲过的技巧：`fetch_and` 必须保护指针高位不被清零，所以把 `val` 的高位全置 1（`| !low_bits`），只让低位（tag 区）参与与运算。`fetch_or` / `fetch_xor` 反过来用 `val & low_bits`（[src/atomic.rs:690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L690)、[src/atomic.rs:728](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L728)），因为 or/xor 本来就只影响被置 1 的位，把高位清 0 即可。

接着看 miri 分支（[src/atomic.rs:657-L660](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L657-L660)）：直接对 `self.data: AtomicPtr<()>` 调 `fetch_and`。`AtomicPtr::fetch_and` 是 1.91 稳定的、strict-provenance 友好的操作——它对指针做位运算时**保留 provenance**（位运算只改地址位，来源元数据不丢），Miri 不会报 UB。

非 miri 分支（[src/atomic.rs:661-L667](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L661-L667)）：把 `&self.data`（`*const AtomicPtr<()>`）cast 成 `*const AtomicUsize`，解引用后调 `AtomicUsize::fetch_and`。这里把指针当成整数来 RMW，是 permissive provenance 风格——在真实 64 位硬件上完全正确（地址就是整数），但不通过 strict provenance 检查，所以**不能**在 miri 下用。`AtomicUsize` 的导入也相应地条件化：[src/atomic.rs:14-L15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L14-L15) `#[cfg(not(miri))] use ... AtomicUsize;`，miri 下根本不引入这个名字。

`fetch_or`（[src/atomic.rs:689-L706](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L689-L706)）与 `fetch_xor`（[src/atomic.rs:727-L744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L727-L744)）结构与 `fetch_and` 完全同构，三处都贴了相同的 1.91/strict-provenance 注释。

把 `without_provenance_mut` 与 `fetch_*` 放在一起看，就能总结 crossbeam 的 provenance 策略：

| 场景 | 意图 | miri 写法 | 非 miri 写法 |
|------|------|----------|-------------|
| 造 ZST 哨兵指针 | **故意**丢 provenance | `transmute(addr)` | `addr as *mut T` |
| 指针 tag 位运算 | **尽量**保 provenance | `AtomicPtr::fetch_*` | `AtomicUsize::fetch_*`（cast） |

二者方向相反：一个想丢、一个想保，但都用「miri 下走严格、非 miri 下走宽松/兼容」的同一种 cfg 分叉手法。

#### 4.4.4 代码实践：解释 `fetch_and` 的 miri / 非 miri 分叉

**实践目标**：把 spec 要求的解释写成你自己的话——「为什么 `fetch_and` 在 miri 下用 `AtomicPtr::fetch_and`、非 miri 下用 `AtomicUsize::fetch_and`」。

**操作步骤**（写一份「解释卡片」，并辅以一次可选的 Miri 验证）：

1. 先阅读 [src/atomic.rs:651-L668](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L668) 与注释 [src/atomic.rs:653-L656](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L653-L656)。
2. 用不超过 150 字写下你的解释，要点应包含：
   - `AtomicPtr::fetch_and` 是 **strict-provenance 兼容**的（位运算保留指针来源），但**需要 Rust 1.91**，高于本 crate 的 MSRV 1.74。
   - 因此生产构建（非 miri）退回把 `AtomicPtr` cast 成 `AtomicUsize` 再做位运算——这是 **permissive-provenance** 风格，在真实硬件上 sound，但丢失了 provenance，通不过 Miri 的严格检查。
   - Miri 构建专门切到 `AtomicPtr::fetch_and`，确保模型检验在 strict provenance 下进行，从而在测试期就能暴露潜在的 CHERI / 严格模型问题。
3. （可选，待本地验证）若有 Miri 工具链，可写一个最小用例：

   ```rust
   // 示例代码
   use crossbeam_epoch::{self as epoch, Atomic, Shared};
   use std::sync::atomic::Ordering::SeqCst;
   let a = Atomic::<i32>::from(Shared::null().with_tag(3));
   let g = &epoch::pin();
   assert_eq!(a.fetch_and(2, SeqCst, g).tag(), 3);
   assert_eq!(a.load(SeqCst, g).tag(), 2);
   # unsafe { drop(a.into_owned()); }
   ```

   用 `cargo +nightly miri run` 跑，观察走的是 `AtomicPtr` 分支且无 provenance UB；用普通 `cargo run` 跑则走 `AtomicUsize` 分支。

**需要观察的现象**：

- 解释中应点出「1.91 vs MSRV 1.74」与「strict vs permissive provenance」这一对矛盾。
- Miri 运行无 UB 报告（若能跑）。

**预期结果**：你能清晰说明「miri 分支为了正确性（strict provenance），非 miri 分支为了兼容性（MSRV）」这一权衡，并指出注释里「permissive-provenance compatible and is sound」的含义——非 miri 写法虽然在严格模型下不达标，但因为它只改低位 tag、高位指针地址不变，所以在 permissive 模型下依然安全。

**待本地验证**：Miri 与 nightly 工具链的可用性取决于本机环境；若不可用，请以「源码阅读 + 注释解读」完成本实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fetch_and` 用 `val | !low_bits::<T>()`，而 `fetch_or` 用 `val & low_bits::<T>()`？
**答案**：`fetch_and` 是按位与，若 `val` 高位有 0，会把指针高位地址清零、破坏指针。故先把 `val` 高位全置 1（`| !low_bits`），让与运算只作用于低位 tag 区。`fetch_or` 是按位或，本身就只会「置 1」不会「清零」，所以反向把 `val` 高位清 0（`& low_bits`），确保只动 tag 低位。两者目标一致：**保护指针高位、只改 tag 低位**。

**练习 2**：非 miri 下把 `AtomicPtr` cast 成 `AtomicUsize` 再 `fetch_and`，为什么注释说它「sound」？
**答案**：因为 `fetch_and` 经过 `val | !low_bits` 预处理后，与运算不会改变指针的高位地址（高位全 1 相与等于原值），只改低位 tag。在 permissive provenance 模型下，指针地址不变即等价于同一个指针，故解引用仍合法。换言之，它「丢失了 provenance 元数据」但「没有改变地址」，所以在 permissive 模型下安全；只是经不起 strict provenance / Miri 的审视。

**练习 3**：`without_provenance_mut` 与 `fetch_and` 都用了 miri / 非 miri 分叉，但意图相反。请各用一句话概括。
**答案**：`without_provenance_mut` 在 miri 下用 `transmute` **主动声明丢失 provenance**（造 ZST 哨兵），非 miri 下用 `as` 兼容 CHERI；`fetch_and` 在 miri 下用 `AtomicPtr` **主动保留 provenance**（满足严格模型），非 miri 下用 `AtomicUsize` cast 兼容 MSRV 1.74。一个想丢、一个想保，但都靠 cfg(miri) 切到「更严格的那一条」。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个**「可移植性巡检」**任务：假设你要给 crossbeam-epoch 升级 MSRV 到 1.91，请评估它对 `fetch_and/or/xor` 实现的影响，并验证 `primitive` 抽象层与 `const_fn!` 宏是否仍需要。

**任务步骤**：

1. **定位所有 miri / 非 miri 分叉点**。用 `grep` 在 `src/` 下搜索 `#[cfg(miri)]` 与 `#[cfg(not(miri))]`，列出每处：`fetch_and` / `fetch_or` / `fetch_xor`（[src/atomic.rs:657](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L657)、[src/atomic.rs:695](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L695)、[src/atomic.rs:733](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L733)）与 `AtomicUsize` 导入（[src/atomic.rs:14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L14)）、`without_provenance_mut`（[src/alloc_helper.rs:76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L76)）。
2. **评估 MSRV 升级的影响**：若 MSRV 升到 1.91，`AtomicPtr::fetch_*` 已稳定，则 `fetch_and/or/xor` 可以**统一**用 `AtomicPtr::fetch_*`，删掉非 miri 的 `AtomicUsize` cast 分支（注释 [src/atomic.rs:653-L656](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L653-L656) 明说「Ideally, we would always use AtomicPtr::fetch_*」）。请用一段话说明这样做的好处（全平台 strict-provenance 兼容）与代价（放弃旧 MSRV 用户）。
3. **检查 `primitive` 与 `const_fn!` 是否仍需要**：升级 MSRV **不影响** loom 的 API 差异（loom 的 `UnsafeCell`/`AtomicPtr::new` 仍与标准库不同），所以 `primitive` 抽象层与 `const_fn!` 宏**仍然必需**——它们解决的是 loom 兼容，与 MSRV 无关。请确认这一点，避免误删。
4. **手写巡检报告**（文字即可）：列出「升级后会简化」「升级后不变」「与 MSRV 无关」三类清单各至少一项。

**预期结果**：你能区分清楚哪些分叉是「为 MSRV」（provenance 相关，升级后可统一）、哪些是「为 loom」（primitive / const_fn，与 MSRV 无关，必须保留）。这正是本讲想传递的核心判断力：**移植性的不同维度彼此正交，不能用一把 cfg 解决所有问题。**

> 本实践为源码阅读 + 评估型任务，不需要修改源码，也不需要实际运行命令；若要验证 grep 结果，可在 crossbeam-epoch 目录执行只读检索。

## 6. 本讲小结

- crossbeam-epoch 同时面对四个移植维度：`no_std`、`loom`、`miri`、`target_has_atomic`，彼此正交，分别用不同的 `cfg` 处理。
- `primitive` 抽象层（[src/lib.rs:64-L131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L64-L131)）让上层算法对「loom vs 真实环境」无感：loom 分支转发 `loom::*`，真实分支转发标准库并手写 `UnsafeCell` 包装补齐 `with`/`with_mut`。
- `const_fn!` 宏（[src/lib.rs:136-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L136-L151)）用一对互斥 `cfg` 把同一个函数签名在「带 `const`」与「不带 `const`」间展开，解决 loom 的 `AtomicPtr::new` 非 const 的问题。
- `alloc_helper::Global`（[src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs)）是 no_std 下对 `alloc` 原语的薄封装，正确处理 ZST，并被 `Pointable for [MaybeUninit<T>]` 消费。
- provenance 是本讲主线：`without_provenance_mut` 在 miri 下用 `transmute`（主动丢 provenance 造哨兵）、非 miri 下用 `as`（兼容 CHERI）；`fetch_and/or/xor` 在 miri 下用 `AtomicPtr::fetch_*`（保 provenance）、非 miri 下用 `AtomicUsize` cast（兼容 MSRV 1.74，因为 `AtomicPtr::fetch_*` 需 1.91）。
- 所有这些分叉都遵循同一原则：**miri 下走更严格的那条**，从而让模型检验覆盖最苛刻的内存模型。

## 7. 下一步学习建议

- **横向对照**：阅读 crossbeam-utils 的 `atomic` 模块（本 crate 依赖的 `crossbeam_utils::atomic::AtomicConsume`，见 [src/atomic.rs:12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L12)），看它如何用类似的 `cfg` 抽象处理 consume ordering 与平台差异。
- **回到 EBR 主链路**：本讲是基础设施层，建议结合 u5-l18（pin/unpin 与内存屏障）回看——`primitive::sync::atomic::fence` / `compiler_fence` 正是 u5-l18 讲的 `SeqCst` 屏障与 x86 hack 的实际来源。
- **测试与验证**：继续阅读 u6-l23（测试、基准与示例），看 loom 模型检验（`tests/loom.rs`）与 examples 压测如何实际启用本讲讲的 `crossbeam_loom` / `miri` / `crossbeam_sanitize_thread` 这些 cfg。
- **延伸阅读**：[Rust strict provenance 跟踪议题](https://github.com/rust-lang/rust/issues/95228) 与 [loom 文档](https://github.com/tokio-rs/loom#handling-loom-api-differences)，理解本讲两处「双实现」背后的官方立场。
