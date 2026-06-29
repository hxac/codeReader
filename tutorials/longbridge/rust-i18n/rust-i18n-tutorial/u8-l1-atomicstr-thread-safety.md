# 全局 locale 与 AtomicStr 线程安全

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `rust_i18n` 是怎样在「全局只有一个变量」的情况下，把当前 locale 暴露给所有线程的 `t!` 宏的。
2. 理解 `LazyLock<AtomicStr>` 这一行声明同时解决了「延迟初始化」与「跨线程共享」两个问题。
3. 掌握 `AtomicStr` 内部用 `arc-swap` 的 `ArcSwapAny<Arc<String>>` 实现「无锁原子读写」的原理——为什么一个线程高频 `set_locale`、另一个线程高频 `t!`，读到的永远是一段**完整**的字符串，而不会读到「半截」。
4. 解释 `locale()` 为什么不返回 `&str`，而返回 `impl Deref<Target = str>`（一个 `Guard` 守卫），这是避免悬垂引用（use-after-free）的关键设计。

本讲属于「并发、性能与工程实践」单元的入口，承接 [u3-l1（t! 宏的完整调用链）](u3-l1-t-macro-call-chain.md)——那一讲告诉你 `t!` 最终会调用 `crate::_rust_i18n_try_translate(locale, key)`，但「这个 `locale` 从哪儿来、为什么能在多线程下安全使用」正是本讲要回答的问题。

## 2. 前置知识

在阅读本讲前，建议你已经了解：

- **全局静态变量与多线程**：Rust 里一个 `static` 变量被整个程序所有线程共享。如果它的值需要在运行时被修改（比如「当前语言」），就必须用某种**线程安全**的内部可变性机制（`Mutex`、`RwLock`、原子类型等），否则要么编译不过，要么会有数据竞争（data race）。
- **`std::sync::LazyLock`**：Rust 1.80 起进入标准库的「延迟初始化」容器。包裹一个闭包，**第一次**访问时才执行闭包初始化，且初始化只发生一次、对其它线程可见。它替代了旧版的 `lazy_static!` 宏。
- **`Arc`（原子引用计数）**：`Arc<T>` 是线程安全版本的 `Rc<T>`，多个线程可以同时持有一个 `Arc`，引用计数用原子操作维护。当最后一个 `Arc` 被销毁，内部数据才被释放。
- **`Deref` 与解引用强制转换**：当一个类型实现了 `Deref<Target = U>`，你就可以把 `&T` 当作 `&U` 来用（编译器自动插入 `*`/解引用），这叫 deref coercion。本讲里 `locale()` 返回的东西正是靠它被当作 `&str` 使用的。
- **「文案即 key」与全局 locale**：在 rust-i18n 里，`t!("hello")` 不带 `locale=` 参数时，会用「当前全局 locale」去查翻译。这个全局 locale 由 `set_locale` 修改、由 `locale()` 读取。

> 术语提示：本讲反复出现「**无锁（lock-free）**」「**RCU（Read-Copy-Update）**」「**守卫（Guard）**」三个词。简单说：无锁指读写不用加互斥锁、用原子操作完成；RCU 是一种「读多写少」的并发模式——读端拿到旧数据的引用继续用，写端造一份新数据再原子地「换指针」，旧数据等所有读端都放手了才回收；Guard 则是「持有旧数据引用、保证它不被回收」的小对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/lib.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate，声明全局静态量 `CURRENT_LOCALE`，并定义 `set_locale` / `locale` 两个公共函数。 |
| [`crates/support/src/atomic_str.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs) | 定义 `AtomicStr`（线程安全的字符串）和内部的 `GuardedStr`（守卫视图）。本讲的核心文件。 |
| [`tests/multi_threading.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs) | 多线程压力测试：一边狂调 `set_locale`，一边（多个线程）狂调 `t!`，验证没有数据竞争、读到完整字符串。 |
| [`crates/macro/src/tr.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs) | `_tr!` 过程宏，在生成代码时把默认 locale 取成 `&rust_i18n::locale()`——这是 `locale()` 进入 `t!` 调用链的入口。 |
| [`Cargo.toml`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml) | workspace 级依赖声明：`arc-swap` 与 `triomphe`（带 `arc-swap` feature）。 |

## 4. 核心概念与源码讲解

本讲按「从全局变量 → 存储机制 → 读取守卫 → 读写函数」的顺序拆成四个最小模块。前一个模块是后一个的铺垫，建议按顺序阅读。

### 4.1 CURRENT_LOCALE：全局 locale 的静态载体

#### 4.1.1 概念说明

rust-i18n 需要一个「全程序可见、可在运行时改变、默认值是英文」的变量来记录当前 locale。最朴素的写法是 `static CURRENT_LOCALE: &str = "en";`，但 `&str` 是不可变的，无法在运行时 `set_locale`。如果用 `static CURRENT_LOCALE: AtomicStr = ...;` 直接初始化，又要求初始化表达式是 `const`（编译期可求值），而 `AtomicStr` 内部有堆分配，不是 `const`。

所以这里用 `LazyLock<AtomicStr>`：把「需要运行期初始化」的 `AtomicStr` 装进「延迟到首次访问才初始化」的容器，既满足了 `static` 的 `const` 约束，又实现了「用到才初始化、且只初始化一次」。

#### 4.1.2 核心流程

```
程序启动
  └─ CURRENT_LOCALE 尚未初始化（闭包 AtomicStr::from("en") 还没跑）
        │
首次有人访问 CURRENT_LOCALE（比如第一次 set_locale 或 locale()）
  └─ LazyLock 用一次性的同步原语执行闭包
        ├─ 原子地创建 AtomicStr，内部存 "en"
        └─ 把结果固定下来，后续所有线程都看到同一个 AtomicStr
              │
此后任何线程读写 CURRENT_LOCALE
  └─ 直接复用那个已初始化的 AtomicStr（不再跑闭包）
```

关键点：`LazyLock` 保证了「**即使一百个线程同时第一次访问，闭包也只执行一次**」，这是它内部用 `Once`/原子状态机实现的。

#### 4.1.3 源码精读

全局 locale 的声明只有一行，在根 crate 的 `src/lib.rs`：

[src/lib.rs:15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L15) —— 用 `LazyLock` 包裹一个 `AtomicStr`，闭包里把它初始化为 `"en"`：

```rust
static CURRENT_LOCALE: LazyLock<AtomicStr> = LazyLock::new(|| AtomicStr::from("en"));
```

注意它顶部的导入（[src/lib.rs:3](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L3)）同时引入了 `LazyLock` 和 `Deref`（后者供 `locale()` 的返回类型用）：

```rust
use std::{ops::Deref, sync::LazyLock};
```

而 `AtomicStr` 这个类型本身是从 `rust_i18n_support` re-export 进来的（[src/lib.rs:7-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L7-L8)），真正的实现在 support crate（下一节展开）。

`LazyLock` 在这里同时提供了三项保证：

1. **延迟初始化**：闭包 `|| AtomicStr::from("en")` 不在程序启动时跑，省去一次堆分配；直到真正用到 locale 才执行。
2. **线程安全的单次初始化**：多线程同时首次访问也只会创建一个 `AtomicStr`。
3. **`'static` 生命周期**：`static` 变量活到程序结束，所以 `CURRENT_LOCALE` 可以安全地被任何线程随时引用。

#### 4.1.4 代码实践

**实践目标**：确认 `CURRENT_LOCALE` 的「懒初始化」与「默认值」。

**操作步骤**：

1. 打开 `src/lib.rs` 文件末尾的 `#[cfg(test)] mod tests`（[src/lib.rs:199-212](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L199-L212)），阅读 `test_locale`。
2. 该测试断言了两件事：`&locale()` 等于 `&CURRENT_LOCALE.as_str()`，且解引用后等于 `"en"`。

**需要观察的现象**：测试中 `assert_eq!(&*locale(), "en");` 能通过，说明在「没有调用过 `set_locale`」时，`locale()` 拿到的就是闭包里写的默认值 `"en"`。

**预期结果**：`cargo test --lib test_locale` 通过。

**待本地验证**：如果你手动调用 `rust_i18n::set_locale("zh-CN")` 之后再读 `locale()`，应得到 `"zh-CN"`（见 4.4 模块）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能写成 `static CURRENT_LOCALE: AtomicStr = AtomicStr::from("en");`？

> **参考答案**：`static` 变量的初始化器必须是 `const`（编译期常量）。`AtomicStr::from("en")` 内部会做堆分配（构造一个 `Arc<String>`），不是 `const fn`，无法在编译期求值，因此必须用 `LazyLock` 把初始化推迟到运行期。

**练习 2**：`LazyLock::new` 的闭包会被执行多少次？

> **参考答案**：最多一次。即使成百上千个线程同时首次访问 `CURRENT_LOCALE`，`LazyLock` 内部的 `Once` 机制保证闭包只跑一次，之后所有线程共享同一个已初始化的 `AtomicStr`。

### 4.2 AtomicStr 与 ArcSwapAny：无锁原子存储

#### 4.2.1 概念说明

`AtomicStr` 是 rust-i18n 自己造的「线程安全字符串」轮子，目的只有一个：**让「写」端能原子地换掉整个字符串，而「读」端永远拿到一段完整、自洽的字符串**。

它没有用 `Mutex<String>` 或 `RwLock<String>`，因为那些会在每次读写时加锁，对「读多写少」的 locale 场景太重。取而代之的是 [arc-swap](https://docs.rs/arc-swap) 这个 crate 提供的 `ArcSwapAny`：一种基于 RCU 思想的无锁容器，内部存一个 `Arc` 指针，写时「造新的 → 原子换指针」，读时「加载当前指针 → 顺手把引用计数 +1 保活」。

为什么读到的永远是「完整的字符串」而不是「半截」？因为 `String` 在堆上是一块**不可变**的连续内存，rust-i18n 从来不原地改写它。`set_locale` 每次都是构造一个**全新的** `String`，装进全新的 `Arc`，再用一次原子操作把指针指过去。读端要么拿到换指针之前的旧 `Arc`，要么拿到之后的旧 `Arc`——两种情况都是一段完整的、没有人正在改写的字符串。指针的替换本身是原子的（一条 CPU 指令级别），所以不会出现「读到一半指针变了」的中间态。

#### 4.2.2 核心流程

```
初始：ArcSwapAny 内部指针 -> Arc_0(String="en")

线程 A 调用 set_locale("zh-CN")【写端】
   1. 在堆上分配新字符串 String="zh-CN"
   2. 包成新 Arc_1
   3. ArcSwapAny::store(Arc_1)
        └─ 一条原子指令：内部指针 从 Arc_0 换成 Arc_1

线程 B 调用 locale()【读端】
   1. ArcSwapAny::load()
        ├─ 读取当前指针（原子）
        ├─ 返回一个 Guard，它「租」住当前 Arc，让该 Arc 的引用计数 +1（保活）
   2. GuardedStr 包住这个 Guard，对外表现为 &str
   3. 线程 B 用完后丢掉 Guard -> 引用计数 -1
        └─ 当 Arc_0 没有任何 Guard / Arc 持有时，才真正回收 "en" 那块内存
```

写端开销：构造新 `Arc` + 一次原子 store，复杂度 \(\mathcal{O}(1)\)（不含字符串本身的拷贝）。
读端开销：一次原子 load + 引用计数维护，\(\mathcal{O}(1)\)，且**完全无锁、读端之间互不阻塞**。

这正符合 locale 场景：写极少（偶尔切语言），读极多（每次 `t!` 都要读），无锁读端是性能关键。

#### 4.2.3 源码精读

`AtomicStr` 的全部实现非常短，集中在 [`crates/support/src/atomic_str.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs)。

[crates/support/src/atomic_str.rs:4-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L4-L8) —— 结构体本身就是把 `ArcSwapAny<Arc<String>>` 包了一层；注意这里的 `Arc` 来自 `triomphe`，不是 `std::sync::Arc`：

```rust
use arc_swap::{ArcSwapAny, Guard};
use triomphe::Arc;

/// A thread-safe atomically reference-counting string.
pub struct AtomicStr(ArcSwapAny<Arc<String>>);
```

> 为什么用 `triomphe::Arc` 而非 `std::sync::Arc`？`triomphe` 是标准库 `Arc` 的一个去掉了 weak 计数的精简分支，体积更小、略快。它开启了 `arc-swap` feature（见 [Cargo.toml:49](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L49)：`triomphe = { version = "0.1.11", features = ["arc-swap"] }`），从而能与 `arc-swap` 的 `ArcSwapAny` 配合。依赖 `arc-swap = "1.6.0"` 声明在 [Cargo.toml:23](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L23)。

[crates/support/src/atomic_str.rs:21-38](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L21-L38) —— 三个方法分别是「构造、读、换」：

```rust
impl AtomicStr {
    /// Create a new `AtomicStr` with the given value.
    pub fn new(value: &str) -> Self {
        let arced = Arc::new(value.into());      // 把 &str 变成 Arc<String>
        Self(ArcSwapAny::new(arced))
    }

    /// Get the string slice.
    pub fn as_str(&self) -> impl Deref<Target = str> {
        GuardedStr(self.0.load())                // load 返回 Guard，包成 GuardedStr
    }

    /// Replaces the value at self with src.
    pub fn replace(&self, src: impl Into<String>) {
        let arced = Arc::new(src.into());        // 构造一个全新的 Arc<String>
        self.0.store(arced);                     // 原子地换指针
    }
}
```

- `new`：构造时把字符串装箱成 `Arc<String>`，交给 `ArcSwapAny::new`。
- `as_str`（读端）：调 `self.0.load()` 拿到一个 `Guard`，包进 `GuardedStr` 返回（守卫机制详见 4.3）。
- `replace`（写端）：先 `Arc::new(src.into())` 造一个全新的 `Arc<String>`，再 `store` 原子替换。**注意它接受 `&self`（不可变借用），却改了内容**——这就是「内部可变性」，靠 `ArcSwapAny` 的原子操作实现，不需要 `&mut self`。

`From<&str>` 与 `Display` 实现（[crates/support/src/atomic_str.rs:40-50](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L40-L50)）只是便捷封装，`Display` 内部也是走 `as_str()` 拿到 `&str` 再写。

#### 4.2.4 代码实践

**实践目标**：用 `AtomicStr` 这个纯运行时类型（不依赖 `i18n!`）亲手验证「写端换值、读端读到完整新值」。

**操作步骤**（示例代码，非项目原有代码，需自建一个小测试）：

```rust
// 示例代码：演示 AtomicStr 的无锁读写
use rust_i18n_support::AtomicStr;
use std::thread;

let s = AtomicStr::from("en");
let s_ref = &s;

let writer = thread::spawn(move || {
    for _ in 0..1000 { s_ref.replace("zh-CN"); }   // 高频换
    // 注意：thread::spawn 需 'static，这里仅为示意，正式写法要用 Arc<AtomicStr>
});
// 读者：不断 as_str()，应永远拿到完整字符串
for _ in 0..1000 {
    let _view = s_ref.as_str();   // view deref 出 &str，是一段完整的 "en" 或 "zh-CN"
}
writer.join().unwrap();
```

**需要观察的现象**：无论读写如何交错，每次 `as_str()` 解引用得到的都是完整的 `"en"` 或 `"zh-CN"`，绝不会出现空串、乱码或截断。

**预期结果**：编译通过、运行无 panic、无数据竞争警告。

**待本地验证**：若用 `cargo +nightly miri test` 跑等价的并发测试，应不报 use-after-free 或数据竞争。

#### 4.2.5 小练习与答案

**练习 1**：`replace` 只拿 `&self`，却修改了内部字符串，这是怎么做到的？

> **参考答案**：通过 `ArcSwapAny` 提供的「内部可变性」。`ArcSwapAny` 内部用原子指针（`AtomicPtr`/`AtomicUsize`）保存当前 `Arc` 的地址，`store` 用一条原子指令改这个指针。原子操作不需要 `&mut self`，因此 `replace(&self, ...)` 即可安全修改。

**练习 2**：为什么说读端「永远读到完整的字符串」？如果 `set_locale` 正在换指针，读端会不会读到一半？

> **参考答案**：不会。`String` 是不可变的堆内存，rust-i18n 从不原地改写它；`replace` 总是构造一个全新的 `Arc<String>` 再用一次原子操作换指针。指针替换本身原子（不可分割），读端要么读到换之前的指针、要么读到换之后的指针，二者都指向一段完整字符串，不存在「半换」的中间态。

### 4.3 GuardedStr：为什么 locale() 返回 impl Deref 而非 &str

#### 4.3.1 概念说明

这是本讲最容易踩坑、也最关键的设计。直觉上，`locale()` 应该返回 `&'static str` 或 `&str`，但它返回的是 `impl Deref<Target = str>`——一个**拥有自己数据**的守卫对象。

为什么不返回 `&str`？因为「字符串的生命周期」与「`CURRENT_LOCALE` 这个变量的借用周期」**不一致**。考虑这个危险场景：

- 线程 B 调 `locale()`，假设它返回了 `&'x str`，借自某个临时的字符串。
- 线程 A 立刻调 `set_locale("zh")`，原子地把指针换成了新的 `Arc`。
- 如果线程 B 持有的 `&str` 没有保住那块旧内存的「引用计数」，当没有其它引用时，旧 `Arc<String>`（"en"）会被回收——而线程 B 手里的 `&str` 正指向这块已释放内存。**这就是 use-after-free。**

`arc-swap` 的 `Guard` 正是用来解决这个问题的：`load()` 返回的 `Guard` 会「租住」当前 `Arc`，相当于把它的引用计数 +1，**只要 `Guard` 还活着，对应的 `Arc<String>` 就不会被释放**。`GuardedStr` 就是把这个 `Guard` 包起来、对外伪装成 `&str`。于是 `locale()` 返回的「守卫」**自己持有保活引用**，它的生命周期不依赖 `CURRENT_LOCALE` 的借用，从而彻底避免了悬垂引用。

#### 4.3.2 核心流程

```
locale()
  └─ CURRENT_LOCALE.as_str()
       └─ AtomicStr::as_str()
            └─ self.0.load()   -> arc-swap 返回 Guard<Arc<String>>
                 ├─ Guard 持有对当前 Arc 的「租约」(保活，引用计数+1)
            └─ GuardedStr(Guard)   -> 一个 struct，内部拥有 Guard
       └─ 返回 GuardedStr  (类型: impl Deref<Target = str>)

调用方拿到 guard_view
  └─ &guard_view  经 Deref coercion 变成 &str
       └─ GuardedStr::deref() -> self.0.as_str() -> &str（指向被 Guard 保活的 String）
  └─ 用完，guard_view 离开作用域
       └─ GuardedStr drop -> Guard drop -> 引用计数 -1
            └─ 若此时 Arc 计数归零，才回收那块字符串内存
```

#### 4.3.3 源码精读

[crates/support/src/atomic_str.rs:11-19](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L11-L19) —— `GuardedStr` 是个私有结构体，内部只装一个 `Guard<Arc<String>>`，并为它实现 `Deref<Target = str>`：

```rust
/// A thread-safe view the string that was stored when `AtomicStr::as_str()` was called.
struct GuardedStr(Guard<Arc<String>>);

impl Deref for GuardedStr {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        self.0.as_str()
    }
}
```

逐行拆解：

- `struct GuardedStr(Guard<Arc<String>>);`：守卫持有一个 `Guard`，而 `Guard` 持有对当前 `Arc<String>` 的保活租约。
- `type Target = str;`：把这个结构体「伪装」成 `str`，于是 `&GuardedStr` 能 deref 成 `&str`。
- `self.0.as_str()`：这里发生了一连串自动解引用——`Guard<Arc<String>>` deref 到 `Arc<String>`，再 deref 到 `String`，最终调用 `String::as_str()` 返回 `&str`。返回的这个 `&str` 指向的内存，正是被 `Guard` 保活着的那块 `String`。

而 `as_str()` 把 `GuardedStr` 当作返回值（[crates/support/src/atomic_str.rs:29-31](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L29-L31)）：

```rust
pub fn as_str(&self) -> impl Deref<Target = str> {
    GuardedStr(self.0.load())
}
```

返回类型写成 `impl Deref<Target = str>` 而非具名的 `GuardedStr`，是为了**隐藏内部类型**（`GuardedStr` 是私有的，不对外暴露实现细节），同时通过 trait 约束让调用方能 `&*` 出 `&str`。

> 一个常见疑问：返回 `impl Deref` 而不是 `&str`，会不会让 `t!` 调用变复杂？不会。在 `_tr!` 生成的代码里，默认 locale 被写成 `&rust_i18n::locale()`（[crates/macro/src/tr.rs:414-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L414-L417)），那个 `&` 配合 deref coercion 就把 `&GuardedStr` 自动当成 `&str` 传进了 `_rust_i18n_try_translate`。这个守卫临时变量在整条 `t!` 语句执行期间一直活着，保住了字符串内存。

#### 4.3.4 代码实践

**实践目标**：亲手感受「守卫活着 → 字符串有效；守卫释放 → 可能回收」的关系。

**操作步骤**：

1. 阅读 `atomic_str.rs` 末尾的单元测试（[crates/support/src/atomic_str.rs:52-65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L52-L65)），看 `test_atomic_str` 如何把 `&s.as_str()`（即 `&GuardedStr`）当作 `&str` 传进 `test_str(s: &str)`——这正是 deref coercion 的体现。
2. 思考（不要真去触发 UB）：如果把 `as_str()` 改成直接返回 `&str`、且不返回 Guard，在并发 `set_locale` 下会出什么问题。

**需要观察的现象**：`test_str(&s.as_str());` 能编译通过，证明 `&GuardedStr` 自动转换成了 `&str`。

**预期结果**：`cargo test -p rust-i18n-support test_atomic_str` 通过。

#### 4.3.5 小练习与答案

**练习 1**：假如 `locale()` 直接返回 `&'static str`，会有什么问题？

> **参考答案**：当前 locale 是运行时可变的（`set_locale`），它的值不是编译期常量，根本不可能有 `&'static str`。即使退一步返回借自某处的 `&'a str`，也无法保证在「另一线程 `set_locale` 换指针、旧 Arc 被回收」时这块内存仍有效，会触发 use-after-free。

**练习 2**：`GuardedStr` 为什么被设计成「拥有一个 `Guard`」而不是「借用一个 `&str`」？

> **参考答案**：因为必须由**返回值的拥有者**来保活底层内存。`Guard` 在被持有时会维持对应 `Arc` 的引用计数；把它放进 `GuardedStr` 并随返回值移动出去，就等于「把保活责任交给了调用方」。调用方拿着 `GuardedStr` 期间字符串安全，丢弃它时引用计数归零才回收，这正是 RCU 读端的标准做法。

### 4.4 set_locale 与 locale 的运行时协作

#### 4.4.1 概念说明

`set_locale` 与 `locale` 是面向用户的两个公共函数，分别封装 `AtomicStr` 的写端（`replace`）和读端（`as_str`）。它们本身极薄，但理解它们的协作能帮你串起整条 `t!` 调用链：`set_locale` 改全局 locale → 之后不带 `locale=` 的 `t!` 就用这个新 locale 去查翻译。

需要特别区分两种「指定 locale」的方式（这点承接 u3-l1）：

- **全局 locale**：由 `set_locale` 设置，影响此后所有不带 `locale=` 的 `t!`。这是「进程级」的当前语言。
- **临时 locale**：`t!("key", locale = "fr")` 只影响这一次调用，不改全局。它直接把 `"fr"` 字面量/表达式传给 `_rust_i18n_try_translate`，**根本不碰** `locale()` / `CURRENT_LOCALE`。

#### 4.4.2 核心流程

```
【改全局】
set_locale("zh-CN")
  └─ CURRENT_LOCALE.replace("zh-CN")
       └─ AtomicStr::replace -> Arc::new("zh-CN".to_string()) -> ArcSwapAny::store
            └─ 指针原子换成新 Arc

【读全局（被 t! 使用）】
locale()
  └─ CURRENT_LOCALE.as_str() -> GuardedStr(Guard)   // 返回 impl Deref<Target=str>

【t! 不带 locale= 时】
_tr! 生成的代码：
  let locale 默认 = &rust_i18n::locale();           // 取全局 locale（守卫保活）
  crate::_rust_i18n_try_translate(locale, &msg_key) // 用它去 _RUST_I18N_BACKEND 查
```

#### 4.4.3 源码精读

[src/lib.rs:17-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L17-L25) —— 两个函数都只有一行，分别委托给 `AtomicStr` 的 `replace` 与 `as_str`：

```rust
/// Set current locale
pub fn set_locale(locale: &str) {
    CURRENT_LOCALE.replace(locale);
}

/// Get current locale
pub fn locale() -> impl Deref<Target = str> {
    CURRENT_LOCALE.as_str()
}
```

注意 `locale()` 的返回类型 `impl Deref<Target = str>`，与 4.3 的 `GuardedStr` 对应——它对外只承诺「能 deref 成 `str`」，隐藏了守卫细节。

而 `_tr!` 在生成 `t!` 的展开代码时，对「未显式传 `locale=`」的情况，会把 locale 默认取成全局值，见 [crates/macro/src/tr.rs:414-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L414-L417)：

```rust
let locale = self.locale.map_or_else(
    || quote! { &rust_i18n::locale() },   // 没传 locale= 就用全局 locale
    |locale| quote! { #locale },           // 传了就用调用方给的值
);
```

随后这个 `locale` 被传进查表函数（[crates/macro/src/tr.rs:438](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L438) 与 [crates/macro/src/tr.rs:454](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L454)）：

```rust
if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) { ... }
```

这条调用链的终点 `_rust_i18n_try_translate` 定义在 [crates/macro/src/lib.rs:385-400](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L385-L400)，它会先精确查 `_RUST_I18N_BACKEND.translate(locale, key)`。所以：**全局 locale 的线程安全（`AtomicStr`）直接决定了 `t!` 在并发下的正确性**——这正是本讲存在的意义。

> 旁注：`i18n!` 生成的静态后端 `_RUST_I18N_BACKEND` 在首次初始化时，如果配置了 `default_locale`，会反过来用 `locale()` 读取当前值再决定是否 `set_locale`（[crates/macro/src/lib.rs:301-305](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L301-L305)）。这说明 `locale()` 也在初始化阶段被调用，`LazyLock` 保证此刻 `CURRENT_LOCALE` 已就绪。

#### 4.4.4 代码实践

**实践目标**：区分「全局 locale」与「临时 locale」对 `t!` 的影响。

**操作步骤**：

1. 阅读 `tests/multi_threading.rs` 的 `test_t_concurrent`（[tests/multi_threading.rs:35-73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L35-L73)），观察其中同时出现了不带 locale 的 `t!("hello")`（用全局）和带 locale 的 `t!("hello", locale = &locales[m])`（用临时值）两种写法。
2. 在自己的示例里：先 `set_locale("en")` 调 `t!("hello")`；再 `set_locale("zh-CN")` 调 `t!("hello")`；最后 `t!("hello", locale = "en")`。

**需要观察的现象**：前两次结果随全局 locale 变化；第三次即使全局是 `zh-CN`，也返回 `en` 的译文（临时 locale 不读全局）。

**预期结果**：临时 `locale=` 覆盖当次、不改全局；后续不带 `locale=` 的 `t!` 仍用全局值。

**待本地验证**：上述现象需在有 `en`/`zh-CN` 翻译的环境下运行确认（`tests/locales/` 提供了相关文件）。

#### 4.4.5 小练习与答案

**练习 1**：`set_locale` 是「进程级」的吗？多个线程调用会互相影响吗？

> **参考答案**：是的。`CURRENT_LOCALE` 是全局 `static`，整个进程只有一份当前 locale。任何线程的 `set_locale` 都会改变这个全局值，从而影响此后所有线程里不带 `locale=` 的 `t!`。这既是优点（一处切换、处处生效），也是坑（测试间会相互干扰，所以集成测试套件要求 `RUST_TEST_THREADS=1`，详见 u8-l4）。

**练习 2**：`t!("hello", locale = "fr")` 会修改 `CURRENT_LOCALE` 吗？

> **参考答案**：不会。临时 `locale=` 只是把 `"fr"` 作为参数传给当次的 `_rust_i18n_try_translate`，完全不调用 `set_locale`，全局 locale 原封不动。

## 5. 综合实践

**任务**：阅读 [`tests/multi_threading.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs) 的两个测试，把「为什么一个线程高频 `set_locale`、另一个线程高频 `t!` 不会读到半截字符串」讲清楚，并说明 `locale()` 返回 Guard 守卫在其中扮演的角色。

**操作步骤**：

1. 打开 [tests/multi_threading.rs:8-33](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L8-L33)（`test_load_and_store`），它启动两个线程跑 3 秒：
   - `store` 线程：循环把 locale 设成 `"en-{i}"` / `"fr-{i}"`（`i` 不断递增）。
   - `load` 线程：循环调用 `t!("hello")`。
2. 再看 [tests/multi_threading.rs:35-73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L35-L73)（`test_t_concurrent`），它把读端扩到 4 个线程，且读端还会用 `available_locales!()` 和 `t!("hello", locale = ...)`。
3. 运行测试（注意它是 `[[test]]` 集成测试）：`cargo test --test multi_threading`。

**需要你解释清楚的三件事**（这是本次实践的核心产出）：

- **为什么读不到半截字符串**：`set_locale` → `AtomicStr::replace` 每次都构造一个**全新的** `Arc<String>` 再用一次原子操作换指针；`String` 本身不可变、从不被原地改写，故指针替换是原子的，读端要么拿到换之前的完整 `Arc`、要么拿到换之后的完整 `Arc`，不存在「半换」中间态。
- **为什么旧字符串不会在读端使用中被释放**：`t!` 取默认 locale 时调用 `locale()` → `as_str()` 返回的 `GuardedStr` 持有 arc-swap 的 `Guard`，该 `Guard` 保活当前 `Arc`（引用计数 +1）。只要这次 `t!` 调用还在用这个守卫，对应的 `String` 就不会被回收——哪怕 `set_locale` 已经把全局指针换走了。
- **守卫的生命周期**：`&rust_i18n::locale()` 这个临时守卫在整条 `t!` 语句（包含 `_rust_i18n_try_translate` 调用）执行期间一直存活，语句结束才 drop、才可能让旧 `Arc` 计数归零。这保证了查表期间 locale 字符串始终有效。

**预期结果**：测试连续跑 3 秒不 panic、不触发 UB。若环境允许，可用 `cargo +nightly miri test --test multi_threading` 进一步验证无数据竞争/悬垂访问（**待本地验证**：Miri 对 3 秒长跑测试较慢，可临时把 `Duration::from_secs(3)` 改小）。

**延伸思考**：如果 `locale()` 改成返回 `&'static str` 或借自 `CURRENT_LOCALE` 的 `&str`，这个测试还能在 Miri 下通过吗？（答案：不能，会出现 use-after-free。）

## 6. 本讲小结

- 全局 locale 由 `static CURRENT_LOCALE: LazyLock<AtomicStr>`（[src/lib.rs:15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L15)）承载：`LazyLock` 负责「线程安全的延迟初始化」，`AtomicStr` 负责「线程安全的读写」。
- `AtomicStr` 内部是 `arc-swap` 的 `ArcSwapAny<triomphe::Arc<String>>`（[atomic_str.rs:4-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/atomic_str.rs#L4-L8)）：写端 `replace` 造全新 `Arc` 再原子换指针，读端 `as_str` 原子加载指针——全程无锁，符合「读多写少」的 locale 场景。
- 「读不到半截字符串」的根因：`String` 不可变、从不原地改写，指针替换是单次原子操作，读端只会拿到换前或换后的某个完整 `Arc`。
- `locale()` 返回 `impl Deref<Target = str>`（即 `GuardedStr`）而非 `&str`，是因为返回值必须**自己保活**底层字符串——`GuardedStr` 持有的 arc-swap `Guard` 维持 `Arc` 引用计数，避免在并发 `set_locale` 下出现悬垂引用。
- `set_locale` / `locale`（[src/lib.rs:17-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L17-L25)）只是 `AtomicStr` 的薄封装；不带 `locale=` 的 `t!` 会把 `&rust_i18n::locale()` 作为默认 locale 传入查表链（[tr.rs:414-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L414-L417)），因此全局 locale 的线程安全直接决定 `t!` 的并发正确性。
- `multi_threading.rs` 两个测试用「写线程狂切 locale + 读线程狂 `t!`」的 3 秒压力跑法，从外部验证了上述无锁机制（正式数据竞争检测建议用 Miri/TSan）。

## 7. 下一步学习建议

- **继续本单元**：建议接着读 [u8-l2（性能基准与内存优化）](u8-l2-benchmark-and-optimization.md)，看 `criterion` 基准如何量化 `t!` / `t_with_args` / `t_with_threads` 的开销，以及 `SmallVec`、`Cow` 等零拷贝/少分配优化与本章的无锁 locale 如何共同支撑高性能。
- **测试体系**：`CURRENT_LOCALE` 是进程级共享状态，这也是为什么集成测试套件要 `RUST_TEST_THREADS=1`。详见 [u8-l4（测试体系与质量保障）](u8-l4-test-suite.md)。
- **回看调用链**：若对 `locale()` 如何流入 `_rust_i18n_try_translate`、以及回退链编排仍有疑问，可回到 [u3-l1](u3-l1-t-macro-call-chain.md) 与 [u3-l4（翻译回退机制）](u3-l4-fallback-mechanism.md)。
- **深入 arc-swap**：本讲只用了「行为层」结论。若想彻底弄懂 `Guard` 的保活原理（它为何比直接 `clone` 一个 `Arc` 更省），可阅读 arc-swap 官方文档中关于「lease / RCU」的章节，再回头对照 `GuardedStr` 的实现。
