# 零成本与启用门控设计

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `TimingScope::new` / `with_span` 为什么返回 `Option<Self>`，以及这个返回类型如何让「计时关闭」状态做到**几乎零成本**。
- 解释 **fast path（`is_enabled()` 检查）** 与 `#[inline]` 的配合关系，并能指出哪些函数被刻意「不内联」、为什么。
- 量化分析「禁用态」与「启用态」两种情况下，一次 `timed!(...)` 各自付出哪些运行时开销。
- 用一段推理或一个简易基准，对比 `enable=false` 与 `enable=true` 下大量 `timed!` 调用的开销差距，并据此说明该设计为何适合「默认关闭」的生产场景。

本讲是专家层的第一讲。前面你已经学过 `timed!` 宏的用法（u1-l2）和 `ENABLED`/`EVENTS`/`THREAD_DATA` 这些全局状态的来龙去脉（u2-l2）。本讲不再讲「它怎么用」「数据存哪」，而是回答一个更高层的设计问题：**既然 Typst 在 syntax、eval、layout、render 等十几个 crate 的无数热路径里都撒了 `timed!` 埋点，为什么默认关闭时它几乎不拖慢编译速度？**

答案就藏在 `Option<Self>` 这个返回类型和一组精心的 `#[inline]` 标注里。

## 2. 前置知识

本讲会用到几个 Rust 性能与并发术语，先用大白话过一遍。

### 2.1 回顾：TimingScope 的 RAII 模型（来自 u1-l2）

- `timed!("foo", expr)` 宏把表达式 `expr` 包进一个 `TimingScope`：创建时记一条 `Start`，离开作用域（`Drop`）时记一条 `End`。
- `TimingScope::new(name)` 返回的不是 `TimingScope`，而是 **`Option<TimingScope>`**。这一点是本讲的全部核心，务必记住。

### 2.2 回顾：全局开关 ENABLED（来自 u2-l2）

- 进程级静态量 `ENABLED: AtomicBool`，初值 `false`，由 `enable()` / `disable()` 改写，由 `is_enabled()` 读取。
- 读取用的是 `Ordering::Relaxed`（最便宜的内存序），因为 `ENABLED` 只是一块孤立的开关状态，不负责「发布」其它数据。

### 2.3 内联 `#[inline]`

`#[inline]` 是给编译器的**建议**：把函数体直接展开到调用处，省去一次「调用 + 返回」。它在**跨 crate 调用**时尤为关键——默认情况下，跨 crate 的函数调用不会被内联，因为编译器看不到对方函数体。加了 `#[inline]`，函数体会进入 crate 的元数据（metadata），调用方 crate 才有机会把它展开掉。

### 2.4 「零成本抽象」是什么意思

Rust 常说「零成本抽象（zero-cost abstraction）」：你用起来很方便的抽象，**在不使用它的功能时，不产生运行时开销**。本讲的 `timed!` 就是典型——它是个很方便的埋点宏，但只要你没 `enable()`，它的开销就趋近于一次原子读加一次分支判断。

> 一句话：**好的门控不是「功能关了」，而是「关了之后，关掉的代码几乎不存在」。**

## 3. 本讲源码地图

本讲几乎全部内容集中在 crate 的唯一源码文件里，另需瞄一眼宏的另一入口：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | 全部实现。本讲重点看四处：① 门控函数 `with_span`/`new`；② 真正干活的 `new_impl` 与 `Drop::drop`；③ 一组 `#[inline]` 标注；④ `enable`/`disable`/`is_enabled` 与 `timed!` 宏。 |
| [crates/typst-macros/src/time.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs) | `#[time]` 属性宏的展开逻辑。本讲只用它来证明「两个宏入口走的是同一道门」。 |

## 4. 核心概念与源码讲解

### 4.1 门控点：with_span 返回 Option<TimingScope>

#### 4.1.1 概念说明

「门控（gate）」指的是：在真正干活之前，先检查一个开关；开关关着就直接返回，不进入昂贵的代码路径。

`typst-timing` 的门控**唯一入口**是 `TimingScope::with_span`。它的关键设计是返回 `Option<Self>`：

- 开关关着 → 返回 `None`，**完全不去构造** `TimingScope`。
- 开关开着 → 返回 `Some(TimingScope)`，此时才进入真正会加锁、分配、取时间的 `new_impl`。

为什么 `Option` 能做到「连带结束事件一起关掉」？因为 `timed!` 宏把返回值绑到一个局部变量上：

```rs
// timed!("foo", body) 展开后大致是：
let __scope = TimingScope::new("foo");   // __scope: Option<TimingScope>
body
// 作用域结束：__scope 被销毁
```

- 当 `__scope` 是 `None` 时，`Option<TimingScope>` 的 `Drop` 实现**根本不会调用**内层 `TimingScope::drop`（`Option` 只在 `Some(x)` 时才 drop 内层）。于是「记一条 End」也不会发生。
- 一次门控，同时挡住了 Start 和 End。

这就是「门控完整覆盖一对事件」的精妙之处：你不需要写 `if enabled { start(); ...; end(); }` 这种容易漏掉 `end()`（尤其在提前 return 或 panic 时）的代码，`Option` + RAII 自动帮你把两头都关掉。

#### 4.1.2 核心流程

`timed!("foo", body)` 在两种状态下的执行路径：

```text
                ┌─ is_enabled()? ─┐
timed!("foo",b) │                 │
        ┌───────▼───────┐   ┌─────▼──────────┐
        │ 关闭 (false)  │   │ 开启 (true)    │
        ├───────────────┤   ├────────────────┤
        │ with_span →   │   │ with_span →    │
        │   None        │   │   new_impl(...)│
        │ 不加锁/不分配 │   │   加锁+push    │
        │               │   │   Start 事件   │
        │ 执行 body     │   │ 执行 body      │
        │               │   │ Drop:          │
        │ __scope=None  │   │   加锁+push    │
        │ 无 Drop 调用  │   │   End 事件     │
        └───────────────┘   └────────────────┘
```

要点：**关闭态下，`new_impl` 和 `Drop::drop` 这两段「贵代码」都不会被执行**。门控的物理位置就在 `with_span` 的那一个 `if`。

#### 4.1.3 源码精读

先看门控本身。[crates/typst-timing/src/lib.rs:166-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L166-L177) 是整个设计的「心脏」：

```rs
#[inline]
pub fn with_span(name: &'static str, span: Option<NonZeroU64>) -> Option<Self> {
    if is_enabled() {
        return Some(Self::new_impl(name, span));
    }
    None
}
```

- `if is_enabled()` 这一行就是门。关着时直接 `return None`，**`new_impl` 这一行根本不会被求值**。
- `new(name)` 只是转发：[crates/typst-timing/src/lib.rs:160-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L160-L164) 里 `new` 调 `Self::with_span(name, None)`，所以无论用 `timed!` 的哪种形式，最终都汇入同一道门。

再看「门后面」到底有多贵。[crates/typst-timing/src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) 的 `new_impl`：

```rs
fn new_impl(name: &'static str, span: Option<NonZeroU64>) -> Self {
    let (thread_id, timestamp) =
        THREAD_DATA.with(|data| (data.id, Timestamp::now_with(data)));
    EVENTS.lock().push(Event { /* ... */ });
    Self { name, span, thread_id }
}
```

以及销毁时的 [crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)：

```rs
impl Drop for TimingScope {
    fn drop(&mut self) {
        let timestamp = Timestamp::now();
        EVENTS.lock().push(Event { kind: EventKind::End, /* ... */ });
    }
}
```

把这三段连起来看：门关着时，`THREAD_DATA.with`（线程本地访问）、`Timestamp::now`（取系统时间，native 上是一次系统调用）、`EVENTS.lock()`（互斥锁加解）、`Vec::push`（可能的缓冲区扩容）——这些**一个都不会执行**。门关着时，`with_span` 只做了一次原子读加一次分支判断。

> 一个容易忽略的细节：`name: &'static str`（见 u2-l1）也在帮倒忙往「零成本」靠——即便门开着，记事件也不用堆分配字符串；门关着时，字面量只是个指针，压根不会被解引用。

#### 4.1.4 代码实践

**实践目标：** 不运行代码，纯靠阅读源码，论证「`is_enabled()` 为 false 时，一次 `timed!(...)` 不加锁、不分配、不取时间」。

**操作步骤：**

1. 打开 [crates/typst-timing/src/lib.rs:35-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L35-L44) 的 `timed!` 宏，确认 `timed!("foo", body)` 展开为 `let __scope = TimingScope::new("foo"); body`。
2. 跟进 `new` → `with_span`，确认门在 [L172-L177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L172-L177)。
3. 假设 `is_enabled()` 返回 `false`，列出从 `timed!` 调用到 `__scope` 被销毁之间，**实际执行**的代码路径。
4. 对照 `new_impl`（[L180-L191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191)）和 `Drop::drop`（[L194-L205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)），逐一划掉其中不会被执行的「贵操作」。

**需要观察的现象 / 预期结果：**

- 你的执行清单里应当**只剩**：一次 `ENABLED.load(Ordering::Relaxed)`、一次 `if` 分支（走 `None` 那条）、`body` 本身、以及一个对 `Option::None` 的无操作销毁。
- `EVENTS.lock()`、`Vec::push`、`Timestamp::now`、`THREAD_DATA.with` 都应被划掉。

#### 4.1.5 小练习与答案

**练习 1：** 假如把 `with_span` 的返回类型从 `Option<Self>` 改成 `Self`（始终构造一个 `TimingScope`），并在 `new_impl` 内部用 `if is_enabled()` 决定要不要 push Start，「零成本」会在哪一步破功？

**参考答案：** 即使 Start 被 `if` 挡住，函数仍然返回了一个真实的 `TimingScope` 值，于是 `Drop::drop` **一定会被调用**。`drop` 里又会去 `EVENTS.lock().push` 一条 End——也就是说 End 事件照样会被记录，锁照样会被获取，「零成本」在销毁阶段破功。这正是为什么门控必须用 `Option`：只有 `None` 才能让 `Drop` 整体跳过。

**练习 2：** 为什么门控放在 `with_span`，而不是直接写进 `timed!` 宏（例如 `if is_enabled() { let __scope = ...; } body`）？

**参考答案：** 把门控收敛到一个函数里，能让 `timed!` 和 `#[time]` 两个宏入口共享同一套逻辑（见 4.2）。若写进宏，每个宏分支都要各自实现门控，容易不一致；而且 `Option` + RAII 天然覆盖 End，手写 `if` 则需要在每条退出路径上小心配对 Start/End，既啰嗦又易错。

---

### 4.2 fast path 与 #[inline] 的冷热分离

#### 4.2.1 概念说明

「门」虽然逻辑简单，但它被**散落在十几个 crate 的无数调用点**调用。如果每次调用 `timed!` 都要付出一次「跨 crate 函数调用」的代价，那这个门本身就成了新的开销。

`typst-timing` 的做法是**冷热分离（hot/cold path split）**：

- **热路径（fast path）**：`is_enabled()` 检查 + 分支。这段代码极短，每个调用点都希望它「消失」掉——所以相关函数标了 `#[inline]`，让编译器把它展开进调用方。
- **冷路径（slow path）**：真正干活的 `new_impl` 和 `Drop::drop`（加锁、push、取时间）。这段代码体量大，只在开启计时时才跑——所以**刻意不加** `#[inline]`，让它保持为一个独立函数调用，避免在每个调用点都复制一份，造成代码体积膨胀（code bloat）。

这是一种很经典的优化套路：**把便宜的检查内联进去，把昂贵的实现留在外面**。

#### 4.2.2 核心流程

调用点 `timed!("foo", body)` 经编译器内联后，理想情况下会变成这样的机器码轮廓：

```text
; —— 以下是 with_span / is_enabled 被内联后的结果 ——
load  ENABLED          ; Relaxed 原子读一个字节
test  结果是否为 true
jne   <调用 new_impl>  ; 分支：只在开启时跳转，预测为「不跳」
; —— 关闭态继续执行 ——
执行 body
; __scope 是 None，销毁时无 Drop 调用，不生成任何代码
```

- 因为 `is_enabled()`、`new`、`with_span` 都带 `#[inline]`，跨 crate 调用它们时函数体会被展开，于是「读开关 + 分支」直接出现在调用点。
- `new_impl` 没有标 `#[inline]`，所以它仍是一次真正的函数调用——但这个调用只在分支「跳转」时发生，关闭态永远碰不到。
- 分支预测器会把「跳转到 `new_impl`」预测为不发生（因为绝大多数运行是关闭态），于是关闭态的实际开销 ≈ 一次原子读 + 一条被正确预测的分支。

#### 4.2.3 源码精读

先看「全部内联」的一组开关函数。[crates/typst-timing/src/lib.rs:66-86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L66-L86)：

```rs
#[inline]
pub fn enable() { ENABLED.store(true, Ordering::Relaxed); }

#[inline]
pub fn disable() { ENABLED.store(false, Ordering::Relaxed); }

#[inline]
pub fn is_enabled() -> bool { ENABLED.load(Ordering::Relaxed) }
```

这三个函数都只有一行，且都标了 `#[inline]`——`is_enabled()` 是门控里被频繁读取的那一个，必须能跨 crate 内联。再看门控函数本身，[L160-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L160-L164) 的 `new` 和 [L171-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L171-L177) 的 `with_span` 也都标了 `#[inline]`。

然后注意**对照**：真正干活的 `new_impl`（[L180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180)）和 `impl Drop for TimingScope`（[L194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)）**都没有** `#[inline]` 标注。这不是疏忽，而是刻意：

| 函数 | 是否 `#[inline]` | 为什么 |
| --- | --- | --- |
| `is_enabled` / `enable` / `disable` | 是 | 极短、热路径，跨 crate 也要消失 |
| `new` / `with_span` | 是 | 门控本身，必须内联到调用点 |
| `new_impl` | 否 | 体量大、仅开启时执行，内联会膨胀代码 |
| `Drop::drop` | 否 | 同上，且每个 `timed!` 都会生成销毁点 |
| `export_json` | 否 | 大函数、低频，无需内联 |

> 跨 crate 小贴士：调用方（如 `typst-syntax`、`typst-eval`）和 `typst-timing` 是**不同的 crate**。Rust 默认不会跨 crate 内联；正是因为标了 `#[inline]`，函数体才进入 typst-timing 的元数据，调用方才有机会把门控展开掉。这一步若漏掉，每个 `timed!` 都会退化成一次真实的跨 crate 函数调用。

最后顺带验证「两个宏入口走同一道门」。`#[time]` 属性宏在 [crates/typst-macros/src/time.rs:29-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L29-L44) 里生成的也是 `TimingScope::new(...)` 或 `TimingScope::with_span(...)`：

```rs
let construct = match meta.span.as_ref() {
    Some(span) => quote! { with_span(#name, Some(#span.into_raw())) },
    None => quote! { new(#name) },
};
// 在函数体首行插入：let __scope = ::typst_timing::TimingScope::#construct;
```

这意味着无论你用 `timed!` 还是 `#[time]`，都汇入 `with_span` 这同一道门、同一套零成本门控——一处实现，两个入口共享。

#### 4.2.4 代码实践

**实践目标：** 用 `cargo expand`（或手工模拟）亲眼看一次 `timed!` 的展开，并标注其中哪些部分会被内联掉。

**操作步骤：**

1. 如果装了 `cargo-expand`，对一个调用了 `timed!("foo", x + 1)` 的函数运行 `cargo expand`，找到展开后的 `let __scope = TimingScope::new("foo");`。
2. 若没装，对照本节给出的宏源码（[L35-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L35-L44)）手工展开。
3. 在展开结果上画两层标记：用「🔥」标出会被内联消失的调用（`new` → `with_span` → `is_enabled`），用「🧊」标出不会内联、仅开启时才调用的部分（`new_impl`、`drop`）。

**需要观察的现象 / 预期结果：**

- 展开后的 `__scope` 类型是 `Option<TimingScope>`。
- 🔥 的链路最终缩成「读 `ENABLED` + 分支」。
- 🧊 的 `new_impl` 作为函数名保留，不在关闭态被调用。

> 说明：本实践为源码阅读型实践；若未安装 `cargo-expand`，手工展开同样成立，结论一致。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `new_impl` 不标 `#[inline]`？把它也内联会有什么坏处？

**参考答案：** `new_impl` 是冷路径，体量大（线程本地访问、取时间、加锁、push），且几乎每个 `timed!`/`#[time]` 调用点都会引用它。若内联，会把这一大段代码复制到每一个调用点，造成严重的代码体积膨胀（code bloat），反而拖累 icache 命中率。保持「检查内联、实现外联」是更优的体积/速度权衡。

**练习 2：** 在**同一个 crate 内部**调用时，`#[inline]` 是否也必不可少？

**参考答案：** 同 crate 内，编译器本来就有函数体、可以自主决定是否内联，`#[inline]` 的强制力相对弱。但 `typst-timing` 的调用方几乎都在**别的 crate**，跨 crate 才是 `#[inline]` 真正发挥作用的地方——它让函数体随元数据外发，使门控能在调用方被展开。

---

### 4.3 禁用态开销分析与设计取舍

#### 4.3.1 概念说明

前两节我们论证了「关闭态几乎不花钱」。这一节把两种状态的开销放到同一张表里对比，并讨论几个**为什么这么设计**的取舍。

核心结论：`typst-timing` 选择「默认关闭 + 关闭态零成本 + 运行时可开启」，而不是「编译期 feature 开关」或「默认开启」。这个选择是为了同时满足三个相互拉扯的目标：**生产环境零负担、可运行时按需开启、不需要为关闭态付费**。

#### 4.3.2 核心流程

单次 `timed!("foo", body)` 在两种状态下的开销构成：

| 开销项 | 禁用态 (默认) | 启用态 |
| --- | --- | --- |
| 原子读 `ENABLED` | ✅ 1 次 (Relaxed) | ✅ 1 次 |
| 分支判断 | ✅ 1 次（预测不跳） | ✅ 1 次（跳转） |
| 线程本地访问 `THREAD_DATA.with` | ❌ | ✅ 1 次（Start，取 thread_id） |
| 取时间 `Timestamp::now` | ❌ | ✅ 2 次（Start + End） |
| 互斥锁 `EVENTS.lock()` | ❌ | ✅ 2 次（Start push + End push） |
| `Vec::push` / 可能扩容 | ❌ | ✅ 2 次 |
| `Drop::drop` 调用 | ❌（`None` 不 drop） | ✅ 1 次 |

用公式近似每态单次开销（不计 `body` 本身）：

\[
\begin{aligned}
C_{\text{off}} &\approx C_{\text{load}} + C_{\text{branch}} \\
C_{\text{on}}  &\approx C_{\text{load}} + C_{\text{branch}} + 2C_{\text{lock}} + 2C_{\text{push}} + 2C_{\text{time}} + C_{\text{tl}}
\end{aligned}
\]

其中 \(C_{\text{time}}\) 在 native 上主要是 `SystemTime::now()` 的系统调用开销（通常上百纳秒量级），\(C_{\text{lock}}\) 是 parking_lot 互斥锁的加解开销。两者量级都远高于 \(C_{\text{load}}\)，所以 \(C_{\text{on}} \gg C_{\text{off}}\)。这正是「默认必须关闭」的物理原因——Typst 的热路径里 `timed!` 密集到每秒可能触发数百万次，启用态的锁与系统调用会显著拖慢编译。

#### 4.3.3 源码精读

「默认关闭」由初值保证。[crates/typst-timing/src/lib.rs:60-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L60-L61)：

```rs
/// Whether the timer is enabled. Defaults to `false`.
static ENABLED: AtomicBool = AtomicBool::new(false);
```

「运行时可开启」则由 `enable()` 提供（[L67-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L67-L72)），上层（如 typst-kit 的 `Timer`、CLI 的计时开关）在需要时调用一次即可让全进程的所有埋点同时「活过来」。

这里有几个值得点出的**设计取舍**：

1. **为什么用运行时门控，而不是 `#[cfg]` 编译期 feature？**
   Typst 希望计时是一个**运行时开关**（例如 CLI 传一个标志就开始 profiling），而不需要为「带计时 / 不带计时」维护两份编译产物。一个「关闭时近乎免费」的运行时门控，恰好兼得了「运行时灵活」与「默认零成本」。如果用 `#[cfg]`，换一次开关就要全量重编译，对开发者体验很差。

2. **为什么用 `Relaxed` 内存序就够了？**
   因为 `ENABLED` 只是一块孤立状态，它不负责「发布」任何伴随数据（真正的事件数据由 `EVENTS` 的 `Mutex` 自己保证可见性）。这点在 u2-l2 已详细论证，这里只引用结论：只需原子性、不需同步，故选最便宜的 `Relaxed`。

3. **为什么 `name` 用 `&'static str`？**
   关闭态下字面量只是个不被解引用的指针；即便开启态，记事件也不必堆分配字符串（见 u2-l1）。这让「启用态」也尽量便宜、锁内零分配。

#### 4.3.4 代码实践

**实践目标：** 构造一张「开销对比表」，并据此写一段 3–5 句话的推理，说明该设计为何适合「默认关闭」的生产场景。

**操作步骤：**

1. 复制 4.3.2 的开销表，结合本讲对 `new_impl` / `Drop::drop` 的源码阅读，在「启用态」一列补上每项的**来源行号**（例如「取时间 2 次」对应 [L196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L196) 与 [L182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L182)）。
2. 写一段推理：假设 Typst 某次编译会触发 \(10^7\) 次 `timed!`，按表估量禁用态与启用态的总开销量级差异（数量级估算即可）。
3. 给出结论：为什么「默认关闭 + 关闭态零成本」是生产环境的正确默认值。

**需要观察的现象 / 预期结果：**

- 禁用态总开销主要来自 \(10^7\) 次原子读 + 分支，量级在「毫秒以内」。
- 启用态总开销被「锁 + 系统调用」主导，量级可能高出数个数量级，足以让一次编译明显变慢。
- 结论应指向：默认关闭是对的，而门控让「关闭」真正廉价到可以常驻代码库。

> 数量级估算属推理性质，具体数字「待本地验证」——下一节的综合实践会给你一个实测方法。

#### 4.3.5 小练习与答案

**练习 1：** 如果 `ENABLED` 的内存序从 `Relaxed` 改成 `SeqCst`，禁用态会变慢多少？有必要吗？

**参考答案：** 会变慢——`SeqCst` 是最强的内存序，要在多核间维护全局顺序，开销显著高于 `Relaxed`。完全没有必要，因为 `ENABLED` 不发布任何伴随数据，强序只带来成本不带来正确性收益。

**练习 2：** 假如有人提议「干脆默认开启，让用户随时能看时间轴」，你会用本节的哪条论据反对？

**参考答案：** 用开销表：启用态每次 `timed!` 多出两次锁、两次系统调用、两次 push，而 Typst 单次编译会触发海量埋点。默认开启会让每一次普通编译都背上 profiling 的代价，违背「不给不需要的功能付费」的零成本原则。正确做法是默认关闭、按需 `enable()`。

---

## 5. 综合实践：禁用态 vs 启用态的简易基准

本任务把前三节串起来：亲手测一次「关闭态零成本、开启态昂贵」的差距，验证门控的效果。这是一个**可运行**的实践。

**实践目标：** 在 release 模式下，分别测量 `enable=false` 与 `enable=true` 时，跑完相同数量 `timed!` 调用的耗时，对比两者并解释差异。

**操作步骤：**

1. 在 typst 仓库根目录新建一个临时二进制 crate（或任意能以路径依赖 `typst-timing` 的位置），`Cargo.toml` 写：

   ```toml
   [package]
   name = "timing-bench"
   version = "0.0.0"
   edition = "2021"

   [dependencies]
   # 路径按你放置本 crate 的位置调整，指向 crates/typst-timing
   typst-timing = { path = "../crates/typst-timing" }
   ```

2. `src/main.rs` 写：

   ```rs
   use std::hint::black_box;
   use std::time::Instant;
   use typst_timing::{disable, enable, is_enabled, timed};

   // 用 black_box 防止编译器把 work() 整体优化掉。
   #[inline(never)]
   fn work() -> u64 {
       let mut x = 0u64;
       for i in 0..32 {
           x = x.wrapping_add(i * 7);
       }
       x
   }

   fn run(n: u64) -> u64 {
       let mut acc = 0u64;
       for _ in 0..n {
           acc = acc.wrapping_add(timed!("op", black_box(work())));
       }
       acc
   }

   fn main() {
       let mode = std::env::args().nth(1).unwrap_or_else(|| "off".to_string());
       let n: u64 = std::env::args()
           .nth(2)
           .and_then(|s| s.parse().ok())
           .unwrap_or(500_000);

       match mode.as_str() {
           "on" => enable(),
           _ => disable(),
       }
       println!("is_enabled = {}", is_enabled());

       // 预热，避免首次运行的各种一次性开销污染测量。
       let warm = run(10_000);

       let start = Instant::now();
       let acc = run(n);
       let elapsed = start.elapsed();

       println!("warm={warm} acc={acc}");
       println!("total = {elapsed:?}");
       println!("per-op ≈ {:.2} ns", elapsed.as_nanos() as f64 / n as f64);
   }
   ```

3. 分别运行两次（务必用 `--release`，否则 debug 的内联与优化都不达标）：

   ```bash
   cargo run --release -- off 500000
   cargo run --release -- on 500000
   ```

**需要观察的现象：**

- 两次输出里 `work()` 的开销相同；**差异完全来自 `timed!` 的门控与记录**。
- `off` 的 per-op 开销应非常小（基本是 `work()` 本身 + 一次原子读 + 分支）。
- `on` 的 per-op 开销应明显更大，多出来的部分对应 4.3.2 表中的「锁 + 取时间 + push」。

**预期结果：**

- `off` 的 per-op 应与「裸调 `work()`」几乎持平（待本地验证具体纳秒数）。
- `on` 比 `off` 高出的那部分，即为单次计时作用域的真实成本。
- 两者的比值直观体现了「门控」的价值：默认关闭时，遍布全代码库的埋点几乎不拖慢编译。

> 注意：`on` 模式会累积约 `2 × n` 条事件到全局 `EVENTS`，`n=500000` 时约占几十 MB 内存；若内存吃紧请调小第二个参数，但不要在循环里调 `clear()`，否则会引入额外的锁开销、污染测量。具体数值「待本地验证」。

## 6. 本讲小结

- 门控的唯一入口是 `with_span`，它返回 `Option<Self>`：`is_enabled()` 为假时直接 `return None`，**`new_impl` 这段「贵代码」根本不被求值**。
- `Option` + RAII 让门控**一次覆盖一对事件**——`None` 不触发 `Drop::drop`，所以 Start 和 End 被同时挡住，且天然异常安全。
- 采用**冷热分离**：`is_enabled`/`new`/`with_span` 等短小的门控函数标 `#[inline]`，跨 crate 也能展开进调用点；`new_impl`/`Drop::drop` 体量大、刻意不内联，避免代码膨胀。
- 关闭态开销 ≈ 一次 `Relaxed` 原子读 + 一次被正确预测的分支；启用态多出两次锁、两次取时间、两次 push，二者量级相差悬殊。
- 选择「默认关闭 + 关闭态零成本 + 运行时可开启」，是为了在不维护两份编译产物的前提下，让生产环境的默认编译不被 profiling 拖慢。

## 7. 下一步学习建议

- 下一讲 **u3-l2《宏的两种入口：timed! 宏与 #[time] 属性宏》** 会深入对照 `timed!`（`macro_rules!`）和 `#[time]`（proc-macro）的展开细节，并解释 `span` 为什么用 `into_raw()` 取裸数值——本讲已确认两者都汇入同一道门，下一讲讲它们的差异。
- 若你对门控的底层感兴趣，建议读 `Ordering::Relaxed` 的官方文档与 parking_lot 的 `Mutex` 实现，理解「为何锁开销远高于一次原子读」。
- 想看门控在真实产品里如何被触发，可跳到 **u3-l4《集成实践：typst-kit Timer 与端到端计时导出》**，那里串联了 `enable → clear → record → export_json` 的完整时序。
