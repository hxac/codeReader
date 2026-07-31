# 性能与并发：comemo、rayon、LazyHash 与 singleton!

## 1. 本讲目标

Typst 要做到「改一个字，瞬间重排」，靠的不是某一个银弹，而是四套贯穿全 crate 的性能手段协同工作。本讲把这四套手段集中拆开讲清楚。学完后你应当能够：

- 说清 **comemo** 的 `#[comemo::memoize]` 与 `#[comemo::track]` 各自做什么、它们如何构成增量编译的根基；
- 读懂 `Engine::parallelize` 如何用 rayon 在遵守 comemo 单线程约束的前提下并行排版；
- 解释 **LazyHash** 为什么被全 crate 大量使用、它的「按哈希判等 + 惰性缓存」如何把重复哈希成本压到 O(1)；
- 列举 **singleton!** 宏的典型用途，理解为何大量「空/零/标记」元素要用全局共享单例。

本讲是第 12 单元（高级与扩展）的性能收口篇，依赖 u5-l2（Engine/Sink/Route）与 u9-l3（Introspector 与收敛循环）已建立的认识。

## 2. 前置知识

阅读本讲前，最好先建立以下直觉（对应前置讲义）：

- **增量编译的核心矛盾**：Typst 在「收敛循环」里要反复编译同一篇文档（见 u9-l3）。如果每一轮都把整棵 Content 树、整张样式表重新算一遍，增量就无从谈起。我们需要一种机制，让「输入没变的计算」自动跳过——这就是 comemo。
- **Engine 是手提箱**：u5-l2 讲过，`Engine` 聚合 `world`/`library`/`introspector`/`traced`/`sink`/`route` 六项数据，以 `&mut Engine` 一路传递。其中的 `sink: TrackedMut<Sink>` 是 comemo 追踪的可变引用，这决定了「为什么并行要给每个任务单独建一个 Sink」。
- **哈希是缓存键**：comemo 用参数的哈希做缓存键。所以「能不能快速算出哈希」直接决定「缓存查得快不快」——这正是 LazyHash 要解决的问题。

> 名词速查：**记忆化（memoization）**指把函数调用结果按入参缓存，下次同参直接返回；**增量编译**指只重算受改动影响的部分。comemo 是 Typst 自研的、支持「可追踪（tracked）参数」的记忆化库。

## 3. 本讲源码地图

本讲涉及的源码分两层：行为定义在 `typst-library`，底层工具在 `typst-utils`。

| 文件 | 作用 |
| --- | --- |
| `src/engine.rs` | `Engine` 上下文；`parallelize` 并行入口；`Sink`/`Traced`/`Route` 的 `#[comemo::track]` |
| `src/foundations/styles.rs` | `Styles = EcoVec<LazyHash<Style>>`、`StyleChain` 的指针判等 |
| `src/introspection/counter.rs` | `#[comemo::memoize]` 的真实用例（counter 序列） |
| `src/introspection/convergence.rs` | 收敛判据 `History::converged`（用 128 位哈希比相等） |
| `src/foundations/content/mod.rs` | `Content::empty()` 的 singleton! 用例 |
| `src/math/mod.rs`、`src/layout/page.rs`、`src/text/space.rs` | singleton! 的更多用例 |
| `crates/typst-utils/src/hash.rs` | `LazyHash`/`HashLock` 的定义 |
| `crates/typst-utils/src/macros.rs` | `singleton!` 宏定义 |

> 提示：后两个文件属于 `typst-utils` crate，不在 `typst-library` 内。本讲会跨 crate 引用，因为这两个类型是理解性能手段的根。

## 4. 核心概念与源码讲解

### 4.1 comemo 增量记忆化与 tracked

#### 4.1.1 概念说明

comemo 是 Typst 生态里的记忆化（memoization）库，名字取自「copyable memoization」。它解决的问题是：**编译器里大量函数是「纯函数」（相同输入永远得到相同输出），这些函数没必要重复算**。

comemo 提供两个核心属性宏：

- `#[comemo::memoize]`：标在普通函数上。首次调用时，comemo 把「参数的哈希 → 返回值」存进一张全局表；下次再用**哈希相同**的参数调用，直接返回缓存值，跳过函数体。
- `#[comemo::track]`：标在 `impl` 块或 `trait` 上，生成一组「追踪版」类型（`Tracked`/`TrackedMut`）。被追踪的对象在参与 memoize 时，comemo 不仅看它的哈希，还**记录「这次调用读了它的哪些方法、各自返回了什么」**，把这些记录叫作一个 `Constraint`（约束）。

二者合起来才构成增量编译的完整闭环：

- 用 memoize 跳过没变的纯计算；
- 用 tracked 把「会变的外部世界」（源文件、字体、内省器）包装成可追踪句柄，comemo 据此判断「这次缓存还作不作数」。

#### 4.1.2 核心流程

一个被 memoize 的函数 `f(tracked_world, x)` 的执行流程：

1. comemo 对每个参数算哈希（`tracked_world` 用其句柄身份，`x` 用 `Hash`）；
2. 用这些哈希查缓存表：
   - **命中**：取出上次存的 `Constraint`，重放其中记录的「对 tracked 对象的方法调用」，比对返回值是否都和上次一致；全部一致 → 直接返回缓存结果（**验证通过**）；任一不一致 → 缓存失效，按未命中处理；
   - **未命中**：执行函数体，期间对 tracked 对象的每一次方法调用都被记进新的 `Constraint`，连同返回值一起存表。

关键在于第 2 步的「验证」：增量编译时 world 的某些部分变了（比如某个源文件被改），comemo 重放 tracked 调用、比对返回值，只有真正影响过这次计算的变化才会让缓存失效。这正是 u9-l3 收敛循环里「`constraint.validate` 通过即收敛」的快路径来源。

#### 4.1.3 源码精读

**入口：World trait 用 `#[comemo::track]` 标记**，这是整个增量编译的起点。编译器只持有 `Tracked<dyn World>`，于是 world 的所有方法调用都可被 comemo 记录与重放：

[lib.rs:L59-L67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L59-L67) — `World` trait 顶部标 `#[comemo::track]`，且 `library()`/`book()` 返回 `&LazyHash<...>`（LazyHash 见 4.3）。

**Engine 持有的全是 tracked 句柄**：

[engine.rs:L18-L36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L18-L36) — `Engine` 的 `world`、`introspector`、`traced`、`sink` 四个字段都是 `Tracked`/`TrackedMut`/`Protected<Tracked<...>>`，只有 `library` 是普通引用（因为库本身在编译期间不变）。

**`#[comemo::track]` impl 块的真实用例——Sink**：

[engine.rs:L204-L228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L204-L228) — `Sink` 的 tracked 方法（`introspection`/`delayed_error`/`warn`/`value`）全是 `(&mut self, ..) -> ()`。注意 `warn` 里用 `hash128(&(&warning.span, &warning.message))` 做警告去重——和 LazyHash 同源的 128 位哈希。

**memoize 的真实用例——counter 序列**。这是 comemo 在本 crate 里最典型的「降复杂度」例子。一个计数器在文档里被多处读取，每次读取都要算「从头到当前位置的所有更新」：

[counter.rs:L889-L919](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L889-L919) — `sequence` 是对外薄包装，`sequence_impl` 才是真正标了 `#[comemo::memoize]` 的实现。源码注释点明了收益：「Due to memoization, this has to happen just once for all retrievals of the same counter, cutting down the number of computations from quadratic to linear.」即把多点读值从 \(O(n^2)\) 降到 \(O(n)\)。

`sequence_impl` 的参数列表值得细看：它把 `world`/`library`/`introspector`/`traced`/`sink`/`route` 全部作为**显式参数**传入，正是为了让 comemo 把它们纳入缓存键与 `Constraint`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 memoize 如何把同一计数器的多次读取合并成一次计算。
2. **操作步骤**：
   - 打开 `src/introspection/counter.rs`，在 `sequence_impl`（L915 起）函数体第一行临时加一行 `eprintln!("sequence_impl computed for {:?}", counter);`（仅本地调试，勿提交）。
   - 准备一份多处引用同一计数器的 Typst 文档，例如：
     ```typ
     #let c = counter("demo")
     #c.step()
     #context c.display()  #context c.display()  #context c.display()
     ```
   - 用 `typst compile` 编译一次（命令请在本机执行，结果待本地验证）。
3. **需要观察的现象**：终端里 `sequence_impl computed ...` 的打印次数。
4. **预期结果**：尽管文档里 `c.display()` 出现了三次（三次都要查计数器值），`sequence_impl` 的打印次数应远少于「读值次数 × 迭代轮数」——因为同一 `(counter, selector, ...)` 组合只算一次并缓存。去掉 `eprintln!` 还原源码。
5. 若无法本地编译，明确标注「待本地验证」，改为静态阅读：对照 [counter.rs:L912-L919](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L912-L919)，解释「为什么参数里要带 `library: &LazyHash<Library>`」——答：让 comemo 把库的身份纳入缓存键。

#### 4.1.5 小练习与答案

**练习 1**：`Engine` 的 `library` 字段为什么是 `&'a LazyHash<Library>` 而不是 `Tracked<...>`？
**答案**：标准库在一次编译内不变，没有「被追踪、随迭代变化」的需求；用普通引用即可，省去 tracked 的开销。它外层包 `LazyHash` 是为了让 memoize 函数（如 `sequence_impl`）能廉价地把它纳入哈希键。

**练习 2**：comemo 在缓存命中后为何还要做「验证（validate）」这一步，而不是只比哈希就返回？
**答案**：因为参数里有 tracked 对象（如 `World`）。两个不同的 world 句柄哈希可能相同，但它们背后的源文件内容可能已变。验证阶段重放 tracked 方法调用并比对返回值，确保「缓存所依赖的外部世界」真的没变，从而正确失效过期缓存。

---

### 4.2 rayon 并行：parallelize

#### 4.2.1 概念说明

rayon 是 Rust 生态的数据并行库，把「对集合每个元素做同一件事」自动分摊到多核。Typst 在排版时有很多**互相独立**的子任务（例如各页的排版、各子元素的度量），很适合并行。

但这里有一个张力：comemo 的 `TrackedMut<Sink>` 本质是「单线程的可变借用」，不能让多个线程同时往一个 sink 写。`Engine::parallelize` 就是为此设计的并行入口——它**给每个任务发一个全新的子 Engine 和子 Sink**，各自独立写，最后串行合并回外层 sink。

#### 4.2.2 核心流程

`parallelize<P, I, T, U, F>(iter, f)` 的执行流程：

1. 把输入迭代器 `collect` 成 `Vec<T>`（注释解释：不用 `par_bridge` 是因为它不保序）；
2. `into_par_iter` 并行遍历，**每个元素**在新线程里：
   - 新建一个空 `Sink`；
   - 用 `world`/`introspector`/`traced`/`library`（这些都是 `Copy` 或可重复借用的 tracked 句柄）和**子 sink**、`route.clone()` 拼一个临时 `Engine`；
   - 在这个临时 engine 上跑用户闭包 `f(&mut engine, value)`，得到 `U`；
   - 把 `(U, sink)` 成对收集；
3. 并行结束后，**串行**遍历所有子 sink，用 `self.sink.extend(...)` 把它们的警告/延迟错误/内省记录/跟踪值倒进外层 sink；
4. 返回 `U` 的迭代器（保序）。

注意闭包约束 `F: Fn(&mut Engine, T) -> U + Send + Sync`，以及 `T: Send, U: Send`——这是 rayon 把任务跨线程派发的前提。

#### 4.2.3 源码精读

[engine.rs:L52-L102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L52-L102) — `parallelize` 的完整定义。几个关键点：

- **L65-L67** 先把 `Engine` 解构成各字段，`world`/`introspector`/`traced`/`library` 都是可 `Copy` 复制的 tracked 句柄，能安全地在每个线程里复用；
- **L77-L85** 每个任务 `let mut sink = Sink::new();` 起一个独立 sink，再 `sink.track_mut()` 拼出临时 engine；
- **L83** `route: route.clone()`——`Route` 用 `AtomicUsize` 上界，`Clone` 是深拷贝其 `upper`（见 [engine.rs:L437-L446](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L437-L446)），让每个并行任务各自维护嵌套深度；
- **L91-L99** 收尾串行 `extend`，把子 sink 的四桶内容并回外层（`extend` 内部对 warning 会再走一遍 `warn` 的去重逻辑）。

**调用方在行为 crate**。和本 crate 一贯的「类型在此、行为在外」一致（见 u5-l4 的 Routines），`parallelize` 虽然定义在 `typst-library`，真正的并行排版调用方住在行为 crate：

- [typst-layout/src/pages/mod.rs:L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L185) — 用 `engine.parallelize(...)` 并行处理页面 runs；
- [typst-bundle/src/lib.rs:L180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L180) — 并行处理子元素。

#### 4.2.4 代码实践

1. **实践目标**：理解「每个任务一个子 Sink」的必要性。
2. **操作步骤**：阅读 [engine.rs:L74-L99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L74-L99)。然后在源码阅读层面回答：如果去掉子 sink、让所有任务直接写 `self.sink`，会出什么问题？
3. **需要观察的现象**：用你自己的话描述 `TrackedMut` 的单线程借用约束。
4. **预期结果**：`TrackedMut<Sink>` 是 comemo 给出的「独占可变借用」句柄，无法在多线程间 `Send` 一份可变引用；强行共享会破坏 comemo 的追踪语义（它无法区分来自不同线程的写操作）。`parallelize` 用「每任务独立 sink + 末尾串行 `extend`」既绕开了借用冲突，又让外层 sink 的最终状态确定（合并是顺序的，警告去重结果稳定）。
5. 进一步练习：跟踪 [typst-layout/src/pages/mod.rs:L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L185) 的闭包，确认它返回的类型 `U` 满足 `Send`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `parallelize` 要先把迭代器 `collect` 成 `Vec`，而不是直接 `par_bridge`？
**答案**：源码注释明确说 `par_bridge` 不保留顺序（it does not retain the ordering），而 `parallelize` 要保证返回的 `U` 与输入 `T` 一一对应且保序（最后 `pairs.into_iter()` 按收集顺序返回）。先 collect 再 `into_par_iter` 能保证顺序。

**练习 2**：子 sink 里收集到的「延迟错误（delayed）」会在什么时候被提升为致命错误？
**答案**：`parallelize` 只负责把子 sink 的内容 `extend` 进外层 sink；延迟错误是否提升由收敛循环的驱动方决定——只有在收敛循环结束后仍然存在的延迟错误才被提升为致命错误（详见 u5-l2 的 `Engine::delay` 与 u9-l3）。

---

### 4.3 LazyHash 惰性哈希

#### 4.3.1 概念说明

`LazyHash<T>` 是 `typst-utils` 提供的包装类型：它把一个值 `T` 和「这个值的 128 位哈希」放在一起，哈希**首次需要时才算**，算完缓存起来。它有两个直接收益：

1. **哈希成本从 O(n) 摊到 O(1)**：对一棵大对象（比如整张样式表、整棵 Content 子树），不包装时每次 `hash()` 都要把内容重算一遍；包成 `LazyHash` 后，`hash()` 只是往 hasher 里写一个 128 位整数。
2. **相等判断也变成 O(1)**：`LazyHash` 的 `PartialEq` **按缓存的哈希判等**，两个 `LazyHash` 相等当且仅当其 128 位哈希相等。

为什么这对 Typst 至关重要？因为 **comemo 用参数的哈希做缓存键**——每次查 memoize 缓存都要哈希参数。若参数是个大 `Styles` 列表，不预哈希的话每次查表都 O(n)；预哈希后每次查表只需写一串 128 位整数。

#### 4.3.2 核心流程

`LazyHash` 内部两个字段：

```
hash: HashLock,   // AtomicU128，0 表示尚未计算
value: T,
```

哈希的「惰性」由 `HashLock` 实现：

- `get_or_insert_with(f)`：读取原子值，若是 0（未算）则调用 `f()` 算出哈希并存回，否则直接返回缓存值；
- 用 `Ordering::Relaxed`，因为只需要原子性、不需要同步其他操作；
- `DerefMut` 修改内部值时会先 `hash.reset()`（置 0），让下次哈希重新计算——保证缓存与内容一致。

判等与哈希的关键约束（源码文档反复强调）：**`Hash` 实现必须把所有参与 `PartialEq` 的信息都喂给 hasher**。因为判等靠哈希，若两个语义不同的值哈希相同（哈希碰撞），就会被误判相等。Typst 用高质量 128 位 siphash 把碰撞概率压到可忽略。

> **设计取舍**：源码文档指出，`hash(v)` 与 `hash(LazyHash::new(v))` **不一定相等**——把预算哈希写进 hasher 的输出，和把值的各部分逐个写进 hasher，结果不同。但实践中不会把 `T` 和 `LazyHash<T>` 混用，所以无妨。

#### 4.3.3 源码精读

**LazyHash 与 HashLock 的定义**（在 typst-utils）：

[crates/typst-utils/src/hash.rs:L71-L77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L71-L77) — `LazyHash` 结构，`hash` + `value` 两字段。

[crates/typst-utils/src/hash.rs:L100-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L100-L113) — `load_or_compute_hash` 与 `Hash for LazyHash`：哈希时只 `state.write_u128(缓存值)`，这就是 O(1) 的来源。

[crates/typst-utils/src/hash.rs:L122-L129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L122-L129) — `PartialEq` 按哈希判等。

[crates/typst-utils/src/hash.rs:L236-L250](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L236-L250) — `HashLock::get_or_insert_with` 与 `reset`，惰性 + 修改失效的实现。

[crates/typst-utils/src/hash.rs:L36-L45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L36-L45) — 文档注释点明设计动机：「This is useful if you want to pass large values of `T` to memoized functions. Especially recursive structures like trees benefit from intermediate prehashed nodes.」直接对应 comemo 的缓存键需求。

**在 typst-library 里的典型用法**：

[foundations/styles.rs:L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L25) — `Styles(EcoVec<LazyHash<Style>>)`：样式列表的**每一条** `Style` 都被 `LazyHash` 包住。当 comemo 要哈希一个 `Styles` 时，遍历列表写出每个元素的 128 位缓存值，每条都是 O(1)。

[foundations/styles.rs:L564-L570](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L564-L570) — `StyleChain { head: &'a [LazyHash<Style>], tail }`：零分配的链表视图，`head` 同样是预哈希元素切片。

[foundations/styles.rs:L777-L786](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L777-L786) — `StyleChain` 的 `PartialEq` **按指针判等**（`ptr::eq`），不比较内容。这是为 comemo 量身定制：同一份样式表复用同一片切片，指针相等即身份相等，无需逐元素比哈希。注意这是身份相等（identity），与 LazyHash 的内容相等是两套策略，分别服务于不同场景。

[foundations/bytes.rs:L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/bytes.rs#L46) — `Bytes(Arc<LazyHash<dyn Bytelike>>)`：`LazyHash` 支持 `?Sized` 载荷，包 `dyn Bytelike` trait 对象，配合 `Arc` 让不同来源的字节零拷贝复用且哈希一次。

[engine.rs:L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L26) — `library: &'a LazyHash<Library>`：整个标准库配置预算哈希一次，供 memoize 函数当缓存键。

#### 4.3.4 代码实践

1. **实践目标**：解释「为什么 `Styles` 已派生 `Hash`，还要把每个 `Style` 包进 `LazyHash`」。
2. **操作步骤**：
   - 阅读 [foundations/styles.rs:L24-L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L24-L25)，确认 `Styles` 派生了 `Hash`，但其元素类型是 `LazyHash<Style>` 而非裸 `Style`。
   - 阅读 [hash.rs:L108-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L108-L113)，看 `LazyHash<Style>::hash` 只写一个 `u128`。
3. **需要观察的现象**：如果元素是裸 `Style`，`EcoVec<Style>` 的 `Hash` 会逐条把 `Style` 的全部字段喂给 hasher；包成 `LazyHash<Style>` 后变成逐条写一个 `u128`。
4. **预期结果**：链路是「同一条 `Style` 对象在全文档多处复用 → 首次哈希后其 `HashLock` 缓存住 128 位值 → 此后无论被 comemo 哈希多少次，每条都只花写一个 `u128` 的成本」。这正是「大量类型派生 `Hash`」的根本动因：comemo 需要哈希做键，而 LazyHash 让「提供哈希」变得廉价。
5. 进一步练习：在 [crates/typst-utils/src/hash.rs:L140-L146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/hash.rs#L140-L146) 处确认 `DerefMut` 会 `reset()` 哈希，解释「为什么可变修改后必须重置缓存」。

#### 4.3.5 小练习与答案

**练习 1**：`LazyHash<T>` 的 `PartialEq` 按哈希判等，这对 `T` 的 `Hash` 实现提出了什么硬性要求？
**答案**：`T` 的 `Hash` 实现必须把**所有影响 `PartialEq` 结果的信息**都喂给 hasher。否则会出现「两个语义不等的值恰好哈希相同」被误判相等的错误。源码文档用粗体强调了这一点。

**练习 2**：`StyleChain` 已经有「按指针判等」的 `PartialEq`，为什么还要让 `head`/`Styles` 里的元素是 `LazyHash<Style>`？
**答案**：两件事服务于不同场景。指针判等服务于 comemo 对 `StyleChain` 本身的去重（同一份样式表指针相同）；而 `LazyHash<Style>` 服务于「当 `Style` 作为 memoize 函数的参数被哈希时」的成本控制（例如某 `#[comemo::memoize]` 函数以单条 `Style` 为键）。二者并不冲突，是分层优化。

---

### 4.4 singleton! 单例缓存

#### 4.4.1 概念说明

`singleton!` 是 `typst-utils` 提供的一个极小宏：它声明一个 `static LazyLock<T>`，首次访问时初始化，之后返回指向它的 `&'static T`。用途是给那些**全文档共享、内容不变**的「规范对象」提供一个唯一实例。

Typst 里有大量「空/零/标记」类的元素：空 content、空格元素、数学对齐点、弱分页符、段中断……它们在任何文档里都长得一模一样。如果每次需要都 `new().pack()` 一个新的 `Content`，既浪费堆分配，又让 comemo 缓存里堆满「内容相同但身份不同」的对象（身份不同 = 缓存键不同 = 复用率下降）。用 `singleton!` 把它们规范化为同一个 `&'static` 实例后，所有引用都指向同一地址，comemo 的指针/哈希判等能最大化复用。

#### 4.4.2 核心流程

宏展开非常简单（伪代码）：

```rust
// singleton!($ty, $value) 展开为：
{
    static VALUE: LazyLock<$ty> = LazyLock::new(|| $value);
    &*VALUE
}
```

即：每个调用点生成一个独立的静态变量，首次求值时执行 `$value`，之后永远返回同一引用。

调用模式通常是 `singleton!(Content, XxxElem::new().pack()).clone()`：`singleton!` 拿到 `&'static Content`，`.clone()` 复制出一份_owned_ `Content`。因为 `Content` 内部是 `Arc` 引用计数（见 u3-l1），这个 clone 近乎免费（只增计数），且克隆出来的内容和规范实例**共享同一份堆数据**。

#### 4.4.3 源码精读

**宏定义**（typst-utils）：

[crates/typst-utils/src/macros.rs:L1-L8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L1-L8) — `singleton!` 宏，展开为 `static LazyLock` + `&*VALUE`。

**典型用例一：Content::empty()**。空 content 是最高频的单例：

[foundations/content/mod.rs:L92-L95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L92-L95) — `Content::empty()` 返回 `singleton!(Content, SequenceElem::default().pack()).clone()`。全文档所有「空内容」都共享这一个规范实例。

**典型用例二：数学对齐点**。`&`/`&&` 在数学模式里插入对齐点，文档中出现多少次就引用多少次：

[math/mod.rs:L113-L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L113-L122) — `AlignPointElem::shared()` 用 `singleton!` 返回 `&'static Content`。注意这里返回的是引用而非克隆，调用方按需 `.clone()`。

**典型用例三：弱/边界分页符**：

[layout/page.rs:L581-L594](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/page.rs#L581-L594) — `PagebreakElem::shared_weak()` 与 `shared_boundary()` 分别为「弱分页」「边界分页」提供共享实例，两者都基于 `with_weak(true)`。

**更多同模式用例**（自行对照）：

- [text/space.rs:L12-L17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/space.rs#L12-L17) — `SpaceElem::shared()`，文本空格；
- [text/linebreak.rs:L44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/linebreak.rs#L44) — 手动换行；
- [model/par.rs:L731](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/par.rs#L731) — 段中断 `ParbreakElem`；
- [layout/spacing.rs:L71](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs#L71) — 弱水平间距。

**非 Content 用例**：singleton! 不限于 `Content`。

- [visualize/color.rs:L325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L325) — `pub const MAP: fn() -> Module = || singleton!(Module, map()).clone();`，把颜色模块缓存为单例；
- [foundations/func.rs:L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/func.rs#L249) — `singleton!(CastInfo, ...)`，缓存一个 `CastInfo`；
- [text/mod.rs:L1084](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/mod.rs#L1084) — `singleton!(Vec<FontFamily>, { ... })`，缓存字体回退列表。

#### 4.4.4 代码实践

1. **实践目标**：盘点本 crate 里 singleton! 的全部用途，归纳其适用场景。
2. **操作步骤**：
   - 在仓库根执行（只读检索）：`grep -rn "singleton!" crates/typst-library/src`（命令请在本机执行）。
   - 把命中点按「标记元素 / 配置集合 / 元信息」三类整理成表。
3. **需要观察的现象**：每个命中点构造的值是否具有「全文档不变、可被反复引用」的特征。
4. **预期结果**：singleton! 集中出现在两类地方——(a) 零字段或固定字段的「标记元素」`shared()` 方法（空格、换行、对齐点、弱分页、段中断、空内容）；(b) 构造代价较高且全局唯一的集合/模块（颜色模块、字体回退表、CastInfo）。它们的共同点是：**身份复用能直接转化为 comemo 缓存复用**。
5. 进一步思考：为什么 `AlignPointElem::shared()` 返回 `&'static Content`，而 `Content::empty()` 返回 `Content`（末尾多一个 `.clone()`）？答：`empty()` 的签名是 `fn empty() -> Self` 必须返回 owned；`shared()` 定位为「取共享引用」，由调用方决定是否 clone。

#### 4.4.5 小练习与答案

**练习 1**：如果不用 singleton!，每次需要空格都 `SpaceElem::new().pack()` 新建一个，会对 comemo 缓存造成什么负面影响？
**答案**：每个新建的 `Content` 是新的 `Arc` 实例，身份（指针/地址）不同。comemo 在很多场景按身份或按内容哈希做缓存键，身份不同会导致本可复用的缓存项无法命中，缓存命中率下降、重复计算增多。singleton! 让所有「空格」指向同一地址，最大化复用。

**练习 2**：`singleton!(T, expr)` 里的 `expr` 何时执行？执行几次？
**答案**：在对应 `static LazyLock<T>` 首次被访问时执行（惰性），且因为 `LazyLock` 的同步语义，即使在多线程首次并发访问也只执行一次。

---

## 5. 综合实践

把四套机制串起来，跟踪一次「带计数器的多页文档」的增量编译，逐处标注它用到了哪个机制。

**任务背景**：文档 `#counter("h").step()` 出现在若干标题里，文档被排版成多页，并用 `context counter("h").display()` 读取计数器。

**操作步骤**：

1. **标记元素走 singleton!**：打开文档，确认每个标题、每次 `display()` 周边的「空格」「段中断」「弱分页」等标记元素都来自 [content/mod.rs:L92-L95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L92-L95) 与 [text/space.rs:L12-L17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/space.rs#L12-L17) 的 `shared()`/`empty()`。这些对象全文档共享同一规范实例。
2. **样式走 LazyHash**：标题携带的 `set text` 等样式，经 [foundations/styles.rs:L25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L25) 变成 `EcoVec<LazyHash<Style>>`，每条 Style 的哈希只算一次。
3. **计数器序列走 comemo memoize**：`context counter("h").display()` 最终调 [counter.rs:L913](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L913) 的 `sequence_impl`，同一计数器只算一次序列，多处读值复用。
4. **多页排版走 parallelize**：页面 runs 由 [typst-layout/src/pages/mod.rs:L185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L185) 的 `engine.parallelize(...)` 并行处理，每页一个子 Sink，收尾串行合并。
5. **收敛判据走 128 位哈希**：每轮排版的内省结果，由 [introspection/convergence.rs:L240-L250](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L240-L250) 的 `History::converged` 用 `hash128` 比对最后两轮是否完全一致。

**交付物**：画一张时序图或一张表，把「singleton! → LazyHash → comemo memoize → parallelize → 128 位哈希收敛」这条链路标在对应的源码行上，并各写一句话说明「如果缺了这一环，会退化成什么」。

> 待本地验证项：以上命令与时序若需在本地复现，请用 `typst compile` 配合 `eprintln!` 调试确认；无法本地运行时，按静态阅读方式完成标注即可。

## 6. 本讲小结

- **comemo** 是增量编译的根基：`#[comemo::memoize]` 按参数哈希缓存纯函数结果，`#[comemo::track]` 让 `World`/`Sink`/`Route` 等可变环境可被记录与重放，二者合力实现「输入没变的计算自动跳过」。
- **rayon parallelize** 用「每任务一个子 Sink、收尾串行 `extend`」绕开 comemo 的单线程 `TrackedMut` 约束，把独立的页面/元素排版并行化；其调用方住在 typst-layout/typst-bundle 等行为 crate。
- **LazyHash** 把「提供哈希」的成本压到 O(1)：内部 `HashLock` 惰性缓存 128 位 siphash，`PartialEq` 也按哈希判等；它直接服务于 comemo「按哈希做缓存键」的需求，所以全 crate 大量类型派生 `Hash` 并用 `LazyHash`/`Arc<LazyHash<...>>` 包装。
- **singleton!** 把空格、换行、对齐点、弱分页、空内容等「规范标记」规范化为全局唯一 `&'static` 实例，把「身份复用」转化为 comemo 缓存复用。
- 四套机制并非孤立：singleton! 与 LazyHash 降低「提供缓存键」的成本，comemo 据此跳过重复计算，parallelize 在不破坏 comemo 语义的前提下榨取多核，128 位哈希则用于收敛判据——共同支撑 Typst 的「改一字、瞬间重排」。

## 7. 下一步学习建议

- 若想看 comemo 的「验证（validate）」如何与收敛循环耦合，回看 u9-l3（Introspector 与收敛循环），尤其是 `constraint.validate` 与 `analyze` 的关系。
- 若想动手扩展本 crate，进入 u12-l3（扩展 typst-library：新增元素与函数），那里会用到本讲提到的 `#[comemo::track]`/`LazyHash`/`singleton!` 等设施。
- 若对底层工具感兴趣，可精读 `crates/typst-utils/src/hash.rs` 中 `ManuallyHash`（手动提供哈希的姊妹类型）与 `hash128` 的稳定哈希策略，理解 Typst 为何在 32/64 位架构间要求哈希稳定。
