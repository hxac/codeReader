# Engine、Route、Sink 与 Traced

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `Engine` 聚合了哪六项编译数据，以及它为何是「编译期的中央上下文」。
- 解释 `Sink` 为什么要把 `introspections` / `delayed` / `warnings` / `values` 分成四个桶，以及「延迟错误（delayed error）」的设计动机。
- 理解 `Traced` 如何跟踪某个 `span` 的取值，并知道 `Traced::get` 为什么要按文件 ID 过滤。
- 看懂 `Route` 这条链表如何同时完成「循环导入检测」和「过深嵌套检测」，以及 `upper` 字段带来的剪枝优化。
- 说明 `parallelize` 如何为每个并行任务创建独立的子 `Sink`，再在收尾时合并回外层 `Sink`。

本讲承接 u5-l1（`World` trait 与资源加载）。u5-l1 讲的是「编译器与外部环境的接口」，本讲讲的是「编译过程中来回传递的那只手提箱」——所有动态状态都装在 `Engine` 里，跟随求值、收敛、排版一路传递。

## 2. 前置知识

在进入源码前，先确认几个 u5-l1 已建立、或本讲会反复用到的概念：

- **三支柱**：`World`（外部环境）持有 `Library`（标准库配置），`Engine`（活跃编译上下文）在运行时把它们组合起来。本讲的主角就是第三个支柱 `Engine`。
- **comemo 的 `Tracked` / `TrackedMut`**：Typst 用 comemo 库做增量记忆化。被 `#[comemo::track]` 标注的类型会获得一个「被追踪的引用」`Tracked<T>`（只读）或 `TrackedMut<T>`（可写）。对被追踪对象的方法调用都会被 comemo 记录，从而在下一次编译时判断缓存是否仍然有效。本讲里 `world`、`introspector`、`traced`、`sink` 全是 `Tracked`/`TrackedMut`。
- **`SourceResult<T>`**：Typst 的核心结果类型，`Ok(T)` 或 `Err(EcoVec<SourceDiagnostic>)`（一组带 span 的诊断）。u5-l3 会专门讲诊断系统，本讲只需知道「错误是一组 `SourceDiagnostic`」。
- **`EcoVec`**：引用计数、写时复制的向量，克隆近乎免费（u2-l2 已介绍）。
- **内省（introspection）**：Typst 在排版完成后回填每个元素的位置，用户代码可以查询这些位置（如 `query`、`counter`）。内省结果在排版稳定前会逐次变化，因此需要「收敛循环」反复重排。u9 会深入，本讲只需建立直觉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/engine.rs` | **本讲核心**。定义 `Engine`、`Route`、`Sink`、`Traced` 四个类型，以及 `parallelize` 方法。 |
| `src/introspection/convergence.rs` | 收敛分析：遍历 `Sink` 里记录的内省，生成「document did not converge」诊断。是 `Sink.introspections` 的消费者。 |
| `crates/typst/src/lib.rs`（顶层 `typst` crate） | 编译驱动：跑收敛循环，并在循环结束后把延迟错误「提升（promote）」为致命错误。 |
| `crates/typst-realize/src/lib.rs`（行为 crate） | `Engine::delay` 的真实调用方：应用 show 规则时把错误延迟。 |
| `crates/typst-utils/src/protected.rs` | `Protected<T>` 包装器：强制访问内省器时给出「理由」。 |

> 说明：后三个文件位于 `typst-library` 之外的行为 crate 或工具 crate。本讲以 `engine.rs` 为主线，引用它们只是为了让你看清 `Engine` 的方法在「外面」是如何被调用的——这正是 u1-l1 讲过的「类型在 library、行为在别的 crate」的分工。

## 4. 核心概念与源码讲解

### 4.1 Engine：编译期的中央上下文

#### 4.1.1 概念说明

编译一份 Typst 文档，需要同时携带很多动态信息：当前在哪个 `World`、用哪份标准库、内省器现在给出什么位置、有没有正在被跟踪的 `span`、到目前为止积累了哪些警告和错误、当前调用栈有多深。这些信息如果分散成几十个参数到处传递，函数签名会灾难性地膨胀。

`Engine` 就是把这些动态信息打包成**一个结构体**，作为「手提箱」跟随求值过程传递。任何一个标准库函数或元素在需要编译环境时，都只接收一个 `&mut Engine`。它是编译器的「中央上下文（central compilation context）」——`engine.rs` 第一行文档注释正是这么写的。

#### 4.1.2 核心流程

`Engine` 的生命周期与一次「重排迭代」一致：

```text
顶层 compile() 开始一次迭代
  └─ 构造 Engine {
         world,           // 来自 World trait 对象
         library,         // 来自 world.library()
         introspector,    // 上一次迭代排出的位置（或空）
         traced,          // 可选：要跟踪的 span
         sink,            // 一个全新的空 Sink
         route,           // Route::default()（根路由）
     }
  └─ eval / realize / layout 一路传 &mut Engine
       └─ 任何函数都能：读 world、查 introspector、往 sink 推警告/错误、检查 route 深度
  └─ 迭代结束，从 sink 取出 introspections / delayed / warnings / values 做收尾分析
```

关键点：**每一次收敛迭代都会创建一个全新的 `Engine` 和全新的 `Sink`**（见第 5 节综合实践里的驱动代码）。迭代之间不共享 `Sink`，而是用 `extend_from_sink` 把子结果合并进外层。

#### 4.1.3 源码精读

`Engine` 的定义非常紧凑，六个字段一目了然：

[engine.rs:L18-L36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L18-L36) — `Engine` 结构体定义。逐字段说明：

- `world`：编译环境，`Tracked<dyn World>`。提供 source/file/font 等（u5-l1）。
- `library`：标准库配置，直接缓存成 `&LazyHash<Library>`。注释解释了为什么不通过 `world.library()` 每次取：因为它被访问得**太频繁**，绕开 tracked 调用的开销值得预先取一次。
- `introspector`：内省器，`Protected<Tracked<dyn Introspector>>`。`Protected` 强制调用方在访问时给出一句「理由」（详见 4.1.4），目的就是逼迫所有内省都走 `Engine::introspect` 这条会做记录的路。
- `traced`：可能正在被检视的 `span`，`Tracked<Traced>`（见 4.3）。
- `sink`：纯写入的回收站，`TrackedMut<Sink>`，收集内省、延迟错误、警告、被跟踪的值（见 4.2）。
- `route`：编译走过的路径，`Route<'a>`，用于检测循环导入与过深嵌套（见 4.4）。

`Engine` 自身只暴露三个方法，其余都靠直接读写字段完成：

[engine.rs:L104-L117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L104-L117) — `Engine::introspect`。它把一次内省「执行 + 记录」打包在一起：先 `introspector.access("is okay since we're recording it")` 取出内省器（理由字符串点明了「因为我们正在记录它，所以读这个可能还没收敛的内省器是安全的」），执行 `introspection.introspect(...)`，再把这次内省推进 `sink`。记录的目的，是让收敛分析能事后判断「这次内省是否在多次迭代间稳定」（u9-l3 详讲）。

> 这里的 `Introspect` trait（`type Output: Hash` + `fn introspect` + `fn diagnose`）定义在 [convergence.rs:L93-L140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L93-L140)。它把「一次内省」抽象成一个可哈希、可比较、能产出诊断的对象，是收敛循环的观测单位。本讲只需把它当成「一次会被记录的内省操作」。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Engine` 把六项数据聚合在一起，并理解 `Protected` 的「强制给出理由」机制。

**操作步骤**：

1. 打开 `crates/typst-utils/src/protected.rs`，阅读 `Protected` 的定义。
2. 在 `engine.rs` 里搜索 `.access(`，观察 `Protected` 的访问点。

**需要观察的现象**：

- `Protected<T>` 只是一个 `pub struct Protected<T>(T)` 新类型，[protected.rs:L28-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs#L28-L31) 的 `access` 方法签名是 `fn access(&self, _justification: &'static str) -> &T`——理由参数是 `&'static str`（编译期常量），**运行期根本不检查**。

**预期结果**：你会意识到 `Protected` 是一个「类型系统层面的提醒」而非运行期守卫。它的注释写道：「makes sure that users of the value think twice and justify their use」。把内省器包进 `Protected`，是为了让任何想直接读内省器的代码都被迫写一句理由——而在 `Engine::introspect` 里，这句理由恰恰是「因为我正在记录它」。这就把「读内省器」和「记录内省」在代码层面绑死，避免有人悄悄读内省器却忘了记录，导致 comemo 无法发现未收敛。

#### 4.1.5 小练习与答案

**练习 1**：`library` 字段为什么不是 `Tracked`，而是直接 `&'a LazyHash<Library>`？

> **参考答案**：因为 `library` 在单次编译内不变、且被访问极其频繁。每次走 `world.library()` 这条 tracked 调用都有开销，预先取出一次引用能避免重复开销。`LazyHash` 则保证哈希惰性计算，配合 comemo 仍能正确参与缓存失效判断。

**练习 2**：`world` 是 `Tracked` 而 `route` 不是（`route: Route<'a>`，未被 `Tracked` 包裹）。这两者一个被追踪一个不被追踪，原因分别是什么？

> **参考答案**：`world` 必须是 `Tracked`，因为它是 comemo 增量编译的核心依赖——源文件/字体变了要能让缓存失效。`route` 不需要被 comemo 追踪，它只是一个「当前调用路径」的运行期记录，用于当场检测循环与过深；它的链表结构本身（通过 `Route::track()` 手动管理）就足以在需要时生成可比较的 tracked 句柄。

---

### 4.2 Sink：分桶收集内省、延迟错误、警告与被跟踪值

#### 4.2.1 概念说明

`Sink` 是一个「只写不读（push-only）」的回收站。编译过程中产生的所有「副作用记录」都往这里推：

- **内省（introspections）**：每一次 `Engine::introspect` 的记录，供收敛分析使用。
- **延迟错误（delayed）**：一类特殊的错误——它**先记下来，但不立刻中断编译**。
- **警告（warnings）**：非致命的提示，需要去重。
- **被跟踪的值（values）**：当某个 `span` 正在被检视时，记录该 span 处产生的值与样式。

之所以分桶，是因为这四类数据的「生命周期」和「消费时机」完全不同：内省要等收敛循环结束才分析；延迟错误要等最后一次迭代才决定是否提升；警告要即时去重；被跟踪的值要供 `trace` API 返回。混在一个列表里会很难分别处理。

#### 4.2.2 核心流程

延迟错误是本模块的核心。它的流程是：

```text
某次操作（如应用 show 规则）失败 → 返回 Err(errors)
   │
   ▼ 调用 Engine::delay(result)
   ├─ 若 Ok(v)：直接返回 v
   └─ 若 Err(errors)：把 errors 推进 sink.delayed，返回 T::default()（通常是空内容）
   │
   ▼ 编译继续，用「空内容」顶替，不中断
   │
   ▼ 收敛循环结束（最多 5 次迭代）
   ▼ 驱动代码调用 sink.delayed() 取出全部延迟错误
   └─ 若非空 → 提升为致命错误 Err(delayed)，整个编译失败
```

为什么不能在出错那一刻就中断？因为 Typst 有收敛循环：**早期迭代里内省器还没准备好，show 规则、`counter`、`query` 很可能「暂时」报错**。如果一报错就停，用户会看到一堆「假错误」。延迟错误的策略是：先假装没事、用空内容继续，等内省稳定后的最后一次迭代再判定——只有「连最后一次都还在报」的错误才真正报给用户。这样既避免了误报，又能一次性集中展示所有真正的错误。

#### 4.2.3 源码精读

`Sink` 的四个字段直接对应四个桶：

[engine.rs:L145-L167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L145-L167) — `Sink` 结构体。注意字段上的注释，尤其是 `delayed` 那段（L155-L159）几乎就是延迟错误的完整设计说明：show 规则可能在内省器未就绪的早期迭代抛错，我们先忽略、用空内容继续，**只有当错误在最后一次迭代结束后仍存在，才提升它**。

`delay` 方法是延迟错误的入口，逻辑极简：

[engine.rs:L39-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L39-L50) — `Engine::delay`。`Ok` 原样取出值；`Err` 则调用 `self.sink.delayed_errors(errors)` 把错误塞进 `delayed` 桶，并返回 `T::default()`（`T: Default`）。对调用方而言，`delay` 永远「成功」返回一个值——错误被悄悄存起来了。

`delay` 真正的调用方在行为 crate `typst-realize` 里，应用 show 规则时：

[typst-realize/src/lib.rs:L396-L402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L396-L402) — show 规则出错时不立即终止，而是 `s.engine.delay(result)`，用空内容继续。注释（L396-L401）把动机说得很清楚：这样可以忽略只发生在早期迭代的错误，并一次性集中展示更多有用的错误。

而「提升」发生在顶层驱动的收敛循环之后：

[typst/src/lib.rs:L187-L191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L187-L191) — 收敛循环（L138-L185）结束后，`let delayed = sink.delayed();` 取出全部延迟错误，若非空就 `return Err(delayed)`。这正是「提升为致命错误」的那一步。

`delayed` 桶的写入与读取方法分处两处（一个 tracked、一个普通）：

[engine.rs:L211-L219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L211-L219) — tracked 的 `delayed_error` / `delayed_errors`，编译过程中往桶里推。

[engine.rs:L183-L186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L183-L186) — 普通 getter `delayed(&mut self)`，用 `std::mem::take` 取走并清空桶（所以需要 `&mut self`）。驱动代码用它在循环后取走延迟错误。

警告的去重也值得一提：

[engine.rs:L221-L228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L221-L228) — `warn`。对 `(span, message)` 算 128 位哈希，用 `FxHashSet::insert` 判定是否重复，只有新警告才真正推进 `warnings`。这样即便同一条警告在 5 次迭代里反复产生，最终也只出现一次。

#### 4.2.4 代码实践

**实践目标**：亲手追踪一次延迟错误「从产生到提升」的完整路径。

**操作步骤**：

1. 在 `engine.rs` 中阅读 `Engine::delay`（L39-L50）与 `Sink::delayed_errors`（L217-L219），确认「出错时不中断、只入桶」。
2. 跳到 `crates/typst-realize/src/lib.rs` 第 396-402 行，确认 `delay` 的调用点在「应用 show 规则」之后。
3. 跳到 `crates/typst/src/lib.rs` 第 136-191 行，确认收敛循环（`loop { ... }`）结束后才调用 `sink.delayed()` 并 `return Err(delayed)`。

**需要观察的现象**：

- 在 `typst-realize` 里，show 规则失败的 `result` 经过 `delay` 后立即变成了空内容，后续 `visit_styled` 照常进行——编译没有中断。
- 在 `typst/src/lib.rs` 的循环里，每一次迭代用的是**全新的 `subsink`**（L146），迭代间通过 `sink.extend_from_sink(subsink)`（L159、L177）合并；延迟错误会随合并累积到外层 `sink`。
- 提升语句 `return Err(delayed)` 位于循环之后（L188-L191），即「无论是否收敛，都要先跑完循环再判延迟错误」。

**预期结果**：你能用一句话回答练习题——「延迟错误要到收敛循环结束才提升，是因为早期迭代里内省器未就绪会导致 show 规则等暂时报错；只有撑到最后一次迭代仍在的错误才是真错误。」

#### 4.2.5 小练习与答案

**练习 1**：`Sink::delayed` 这个 getter 为什么是 `&mut self` 而不是 `&self`？它和 `warnings`（`self` 消费）有何不同？

> **参考答案**：`delayed(&mut self)` 内部用 `std::mem::take` 把桶「取走并清空」，需要可变访问。而 `warnings(self)` 是按值消费整个 `Sink`（在驱动结束时调用），不需要保留。设计上，`delayed` 既要读又要清空，故 `&mut self`。

**练习 2**：假设某条 show 规则在第 1、2、3 次迭代都报错，但在第 4 次迭代（收敛了）不再报错。这条错误最终会被报给用户吗？

> **参考答案**：不会。每一次迭代用的是全新 `subsink`，收敛那次迭代（`constraint.validate` 通过、`break`）的 `subsink` 里**没有**这条错误；合并进外层 `sink` 后该错误不存在，`sink.delayed()` 为空，故不提升。这正是延迟错误机制的价值——过滤掉「只发生在早期迭代」的假错误。

---

### 4.3 Traced：跟踪某个 span 的取值

#### 4.3.1 概念说明

Typst 有一个「检视器（inspector）」能力：可以指定源码里的一个 `span`，让编译器把**该 span 处产生的所有值与样式**记录下来。这是 IDE「跳转到值」、调试器观察表达式求值结果等功能的基础。

`Traced` 就是「可能正在被检视的 span」的容器。它要么装着一个 `Span`，要么是默认的空（`None`，表示不跟踪任何东西）。

#### 4.3.2 核心流程

```text
trace(world, span) 被调用
  └─ 构造 Traced::new(span)，传入 compile_impl
       └─ 编译过程中，凡是求值到该 span 的代码
          └─ 调用 engine.sink.value(value, styles) 把值推进 values 桶
  └─ 编译结束，返回 sink.values()
```

被跟踪的值最终收集在 `Sink.values`（4.2 的第四个桶），上限是 `Sink::MAX_VALUES = 10`（[engine.rs:L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L171)），避免一个 span 产生海量值拖垮性能。

#### 4.3.3 源码精读

`Traced` 本身极简：

[engine.rs:L120-L131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L120-L131) — `Traced(Option<Span>)`，`new` 包裹一个待跟踪 span，`default()` 表示不跟踪。

关键在它的 tracked `get` 方法，做了一个按文件过滤：

[engine.rs:L133-L143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L133-L143) — `Traced::get(&self, id: FileId) -> Option<Span>`。只有当被跟踪的 span **属于当前查询的文件**时才返回它，否则返回 `None`。注释点明目的：这样「只有包含被跟踪 span 的那个文件」的结果才会失效，避免改一个文件就让所有文件的检视结果全部失效——这是 comemo 增量编译的精细化控制。

值桶的写入方法：

[engine.rs:L230-L235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L230-L235) — `Sink::value`，在 `MAX_VALUES` 上限内推进 `(Value, Option<Styles>)`。

#### 4.3.4 代码实践

**实践目标**：阅读 `trace` API，理解 `Traced` 如何与 `values` 桶配合返回被跟踪值。

**操作步骤**：

1. 打开 `crates/typst/src/lib.rs`，阅读 `trace` 函数（约 L84-L95）。
2. 对照 `engine.rs` 的 `Traced::new` 与 `Sink::value` / `Sink::values`。

**需要观察的现象**：

- `trace` 用 `Traced::new(span)` 构造跟踪器，调用 `compile_impl`（与 `compile` 共用底层），最后 `sink.values()` 取回结果（[typst/src/lib.rs:L91-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L91-L95)）。
- 它复用了整套编译流程，只是把「sink」换成了一个会收集 `values` 的 sink、把「traced」从默认空换成了带 span 的。

**预期结果**：你会看到 `trace` 与 `compile` 共享 `compile_impl`，差别仅在传入的 `Traced` 和最后读取的桶——`Traced` 是「开关」，`values` 桶是「输出」。

#### 4.3.5 小练习与答案

**练习 1**：`Traced::get` 为什么要按 `FileId` 过滤，而不是无脑返回被跟踪的 span？

> **参考答案**：为了 comemo 缓存的精细化失效。若不过滤，任何文件的改动都可能让「跟踪了 span X」这一约束变化，导致所有依赖该约束的缓存失效。按文件过滤后，只有 span X 所在文件的改动才会触发失效，其余文件的缓存得以保留。

**练习 2**：`values` 桶为什么设 `MAX_VALUES = 10` 的上限？

> **参考答案**：单个 span 可能在循环、递归等场景下被求值非常多次。若无上限，`values` 可能无限增长，拖垮 `trace` 的性能与内存。10 条足以让检视器展示「这个表达式求值成了哪些值」，是个兼顾信息量与成本的折中。

---

### 4.4 Route：循环导入与过深嵌套的检测

#### 4.4.1 概念说明

编译过程是一棵递归的树：求值一个模块会导入别的模块、应用 show 规则会触发新的求值、布局会嵌套布局。这带来两类风险：

- **循环导入**：模块 A import B、B import A，会无限递归。
- **过深嵌套**：用户写出无限自我引用的 show 规则（如 `show: it => it` 配合某些结构），或过深的布局嵌套，会撑爆栈。

`Route` 是一条**链表**，记录「编译器当前走过的路径」。每进入一层（求值模块、调用函数、应用 show 规则、进入布局），就在链表前端加一个段（segment）；退出时丢弃。通过这条链表，既能查「某个文件是否已在链上」（循环检测），又能量「链有多长」（深度检测）。

#### 4.4.2 核心流程

`Route` 的每个段有四个字段，理解它们是理解整个机制的关键：

```text
Route 段 {
    outer: Option<Tracked<Route>>,   // 父段（链表的后继）
    id:   Option<FileId>,            // 本段对应的模块文件（若有）
    len:  usize,                     // 本段的「深度计数」
    upper: AtomicUsize,              // 对「祖先链总长度」的已知上界
}
```

两类检测分别用不同字段：

- **循环导入**：进入模块求值时 `route.with_id(file_id)` 挂上文件 ID；导入新模块前用 `route.contains(target_id)` 沿 `outer` 链查找——找到说明成环。
- **过深嵌套**：进入函数/show/布局时 `route.increase()`，退出时 `decrease()`；用 `check_call_depth` / `check_show_depth` / `check_layout_depth` 判定总深度是否超限。

深度判定的数学表达：设本段深度为 \(l\)（`len`），对祖先链总长度的上界为 \(u\)（`upper`）。若已知

\[ u + l \le D \quad（D 为允许的最大深度） \]

则整条链必然在限内，可直接返回 true 而不必走完整条链——这就是 `upper` 的剪枝优化。

#### 4.4.3 源码精读

`Route` 结构体本身：

[engine.rs:L256-L281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L256-L281) — 四个字段及详尽注释。注意 `upper` 的注释（L275-L280）：我们**故意只维护上界而非精确长度**，因为精确长度会让「在不同但都未超限的深度处」的相同计算无法复用 comemo 缓存。这是个为增量编译量身定的取舍。

构造与变换方法：

[engine.rs:L283-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L283-L334) — 重点看：

- `root()`（L285-L292）：空路由，`len=0`、`upper=0`。
- `extend(outer)`（L295-L302）：在父段前加一段，`len=1`、`upper=usize::MAX`（「我对祖先链长度一无所知」）。
- `with_id(id)`（L305-L307）：挂文件 ID，用于循环检测。
- `unnested()`（L310-L312）：把本段 `len` 置 0。用于「虽然新建了一段、但不希望它算作嵌套」的场景，比如 counter/state 的内省显示——[counter.rs:L931](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L931) 与 [state.rs:L492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L492) 都用了 `Route::extend(route).unnested()`，避免内省显示被误判为过深嵌套。
- `track()`（L318-L323）：智能跳过「不贡献任何东西」的段（无 id 且 len=0）直接返回 `outer`，避免约束链无意义膨胀。
- `increase()` / `decrease()`（L326-L333）：本段深度计数 ±1，show 规则应用时用（见 typst-realize 的 `route.increase()` / `check_show_depth()` / `route.decrease()`）。

四档深度上限及对应检查：

[engine.rs:L336-L394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L336-L394) — 四个常量：`MAX_SHOW_RULE_DEPTH=64`、`MAX_LAYOUT_DEPTH=72`、`MAX_HTML_DEPTH=72`、`MAX_CALL_DEPTH=80`。注释（L336-L339）解释了为什么各不相同：**让 show 规则、布局、调用三类错误拥有不同的「优先级」**。当多种嵌套交错时，阈值低的那类会先触发，从而总是给出最贴切的错误（比如 show 规则自引用时优先报 show 规则错）。每个 `check_*` 方法在超限时用 `bail!` 抛出带 hint 的诊断。

循环检测与深度判定的 tracked 实现：

[engine.rs:L396-L429](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L396-L429) — `contains` 沿 `outer` 递归比对 `id`；`within(depth)` 实现了 4.4.2 的剪枝逻辑：先看 `upper + len ≤ depth` 是否成立以短路（L410-L413），否则递归向 `outer` 查询，并在得到肯定结果时用 `compare_exchange` **收紧** `upper`（L419-L422，且只降不升，故用 CAS 防止误增）。

> `Route` 还手动实现了 `Clone`（[L437-L446](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L437-L446)）：克隆时为 `upper` 新建一个 `AtomicUsize`（复制当前值），而不是复制引用——因为每个克隆出的路由要有自己独立的 `upper` 缓存。

#### 4.4.4 代码实践

**实践目标**：理解 `Route::within` 的剪枝，并验证 `unnested()` 的真实用途。

**操作步骤**：

1. 在 `engine.rs` 阅读 `Route::within`（L404-L428），在纸上模拟一段 `len=3, upper=10`、判定 `depth=20` 的流程：`10+3=13 ≤ 20`，短路返回 true，根本不递归。
2. 在 `src/introspection/counter.rs` 与 `src/introspection/state.rs` 中找到 `Route::extend(route).unnested()` 的调用（L931、L492），阅读上下文，理解为何这两处要把 `len` 置 0。

**需要观察的现象**：

- `within` 在 `upper + len ≤ depth` 时直接返回 true，**完全不触碰 `outer`**——这就是剪枝省下的递归开销。
- counter/state 的显示逻辑会触发新的求值（可能很深），但它们用 `.unnested()` 声明「我这一段不计入嵌套深度」，于是不会把调用方的深度配额白白耗光。

**预期结果**：待本地验证——你可以构造一个会触发 `counter.display()` 的深层嵌套文档，观察它不会因为 counter 的内部求值而过早报「maximum depth exceeded」。若想直接观察 `upper` 的收紧，则需要在 `within` 内加日志（属源码改动，仅建议在本地 fork 中尝试）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Route::extend` 把新段的 `upper` 初始化为 `usize::MAX` 而不是 0？

> **参考答案**：`upper` 是「祖先链总长度的上界」。新建一段时我们对祖先链一无所知，上界应是最宽松的 `usize::MAX`（即「祖先可能无限长」）。若初始化为 0，会错误地断言「祖先链长度为 0」，导致 `within` 假阳性通过。真正的上界会在后续 `within` 调用中通过 `compare_exchange` 逐步收紧。

**练习 2**：四个 `MAX_*_DEPTH` 故意取不同值（64/72/72/80），这个设计的直接收益是什么？

> **参考答案**：当 show 规则、布局、函数调用三类嵌套相互交织时，阈值最低的那类（show 规则，64）会最先触发报错。于是「show 规则自引用」这类问题总是以「maximum show rule depth exceeded」呈现，并附带针对性的 hint（「maybe a show rule matches its own output」），而不是给出一个笼统的「call depth exceeded」。错误更精准。

---

### 4.5 parallelize：为每个并行任务创建子 Sink

#### 4.5.1 概念说明

Typst 的排版是可并行的——比如多页文档的每一页可以同时布局。但并行有个难题：`Engine` 持有 `TrackedMut<Sink>`，而 comemo 的 `TrackedMut` **不允许**在多个线程同时可变借用。

`parallelize` 的解法是「**每个任务一个子 sink，收尾再合并**」：在每个并行任务内部新建一个独立的 `Sink` 并 `track_mut()`，任务结束后把所有子 sink 的四桶内容合并回外层 `sink`。这样每个线程拥有自己的可变 sink，互不冲突，最终结果却像串行一样完整。

#### 4.5.2 核心流程

```text
engine.parallelize(iter, |engine, item| { ... })
  │
  ├─ 1. 把迭代器收集成 Vec（rayon 需要保序）
  │
  ├─ 2. work.into_par_iter().map(|value| {
  │       let mut sink = Sink::new();            // 每个任务一个全新子 sink
  │       let mut engine = Engine {              // 用同一份 world/library/introspector/traced
  │           world, introspector, traced,        //   但 sink 是各自的
  │           sink: sink.track_mut(),
  │           route: route.clone(),               // route 克隆（各自独立 upper）
  │           library, ..
  │       };
  │       (f(&mut engine, value), sink)           // 返回 (输出, 用完的子 sink)
  │   }).collect_into_vec(...)
  │
  └─ 3. 串行遍历结果，把每个子 sink 的四桶内容
          通过 self.sink.extend(...) 合并回外层 sink
```

注意：`route` 用 `route.clone()` 给每个任务一份独立克隆（4.4 提到 Clone 会新建 `upper`），`world`/`library`/`introspector`/`traced` 则是共享的只读引用——它们本就是 `Tracked`/`&`，只读共享是安全的。

#### 4.5.3 源码精读

[engine.rs:L52-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L52-L102) — `parallelize` 全貌。逐段：

- 签名（L53-L64）：泛型很多，本质是「给我一个迭代器 `P` 和一个闭包 `F(&mut Engine, T) -> U`，我并行跑，返回 `U` 的迭代器」。`T`/`U`/`F` 都要求 `Send` 以便跨线程。
- 解构（L65-L67）：把 `self` 拆开，复用各只读字段。
- 收集成 Vec（L69-L71）：注释解释了为何不用 `par_bridge`——它不保序。先 collect 再 `into_par_iter` 能保留顺序。
- 每任务建子 engine（L74-L88）：核心。`let mut sink = Sink::new();` 新建子 sink，构造子 `Engine`（sink 用各自的 `track_mut()`，route 用 `route.clone()`），调用闭包，返回 `(输出, 子 sink)`。
- 合并（L90-L99）：串行 `for` 遍历，对每个子 sink `std::mem::take` 取走内容，调 `self.sink.extend(introspections, delayed, warnings, values)` 合并回外层。
- 返回输出（L101）：把 `(U, Sink)` 拆开，只留 `U`。

合并用的 `extend` 是私有的四参数方法：

[engine.rs:L237-L253](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L237-L253) — 逐桶 extend。注意警告走 `self.warn(warning)`（L247-L249）而非直接 push，从而复用 4.2 的去重逻辑；`values` 则受 `MAX_VALUES` 限制（L250-L252），不会因合并多个子 sink 而溢出。

`parallelize` 的真实调用方在行为 crate，典型是并行布局多页：

[typst-layout/src/pages/mod.rs:L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L185) — 用 `engine.parallelize(...)` 并行处理页面 runs。（另一个调用方在 `typst-bundle`，并行处理子元素。）

#### 4.5.4 代码实践

**实践目标**：理解「子 sink 合并」如何让并行任务的警告/错误/内省不丢失。

**操作步骤**：

1. 阅读 `engine.rs` 第 52-102 行的 `parallelize`，特别关注 L74-L88（建子 engine）与 L90-L99（合并）。
2. 阅读 `Sink::extend`（L237-L253），确认四个桶分别如何合并、警告如何去重、`values` 如何限量。
3. 跳到 `crates/typst-layout/src/pages/mod.rs` 第 185 行附近，观察真实调用如何把「每页布局」作为闭包传入。

**需要观察的现象**：

- 每个并行任务的 `sink` 是**新建的**（`Sink::new()`），与外层 `self.sink` 完全独立——这正是规避 `TrackedMut` 多线程可变借用冲突的关键。
- 合并阶段是**串行**的（`for` 循环），发生在并行计算全部完成之后，因此不会有数据竞争。
- 警告合并走 `warn()` 而非直接 push，所以跨任务的重复警告仍会被去重。

**预期结果**：你能回答「parallelize 如何为每个任务创建子 Sink」——每个任务内 `Sink::new()` + `track_mut()` 构造独立子 engine；任务返回 `(输出, 子 sink)`；收尾时串行 `extend` 把四桶合并回外层 sink，警告复用去重、values 复用上限。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `parallelize` 必须给每个任务新建一个 `Sink`，而不是让所有任务共享外层的 `self.sink`？

> **参考答案**：外层 `self.sink` 是 `TrackedMut<Sink>`，comemo 的 `TrackedMut` 同一时刻只允许一个可变借用，无法跨多个 rayon 工作线程共享写。给每个任务新建独立 `Sink` 并各自 `track_mut()`，让每个线程拥有自己的可变 sink，规避了借用冲突；最终结果通过串行合并保证完整。

**练习 2**：合并子 sink 时，为什么 `warnings` 要逐条走 `self.warn()`，而 `introspections` 和 `delayed` 可以直接 `extend`？

> **参考答案**：`warnings` 需要去重（基于 `(span, message)` 的 128 位哈希），`warn()` 内部含去重逻辑，故逐条调用以复用。`introspections` 和 `delayed` 无需去重（内省天然可能重复记录、延迟错误本就要全量呈现），直接 `extend` 进对应桶即可。

---

## 5. 综合实践

**任务**：把本讲四个组件串起来，画出「一次收敛迭代中 `Engine` 的数据流」。

请阅读顶层驱动的收敛循环 [typst/src/lib.rs:L136-L191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L136-L191)，结合本讲所学，完成下面这张数据流图（用文字或伪代码补全箭头两端）：

```text
每次迭代开始：
  创建 subsink = Sink::new()
  创建 engine = Engine {
      world, library,
      introspector: 上一次迭代的文档位置,
      traced, sink: subsink.track_mut(), route: Route::default()
  }
  document = T::create(&mut engine, ...)
     │  过程中：
     │   - show 规则失败 → engine.delay(result) → 错误进 A 桶，继续用空内容
     │   - 内省发生     → engine.introspect(..) → 记录进 B 桶
     │   - 产生警告     → sink.warn(..)        → 去重后进 C 桶
     │   - 被跟踪 span 求值 → sink.value(..)   → 进 D 桶（上限 10）
     ▼
  constraint.validate(新内省器)？
     ├─ 通过（收敛）：subsink 合并进外层 sink，break
     └─ 未通过：subsink 合并进外层 sink，再迭代一次（最多 5 次）
  
循环结束后：
  delayed = sink.delayed()   ← 取出 A 桶
  若非空 → return Err(delayed)   ← 「提升为致命错误」
  否则   → Ok(document)
```

请回答：

1. A、B、C、D 四个桶分别对应 `Sink` 的哪四个字段？（`delayed` / `introspections` / `warnings` / `values`）
2. 为什么「提升延迟错误」必须发生在 `loop` 之后，而不是某次迭代内部？（联系 4.2 的延迟错误动机）
3. 若某次迭代里调用了 `parallelize` 布局多页，子 sink 的四桶内容最终去了哪里？（联系 4.5 的合并步骤）

> 这是一个纯源码阅读型实践，无需运行。完成后，你应当能向别人讲清：`Engine` 是手提箱、`Sink` 是里面的四格回收站、`Route` 是贴在箱子上的路线条、`Traced` 是「请帮我留意这个标记」的便签、`parallelize` 是「分身术」——每个分身自带一格回收站，回来后统一归档。

## 6. 本讲小结

- `Engine` 是编译期的中央上下文，聚合 `world`/`library`/`introspector`/`traced`/`sink`/`route` 六项数据，作为 `&mut Engine` 在求值链上一路传递。
- `Sink` 是只写回收站，分四桶：`introspections`（收敛分析用）、`delayed`（延迟错误）、`warnings`（带去重）、`values`（被跟踪 span 的值，上限 10）。
- **延迟错误**是核心设计：show 规则等在内省器未就绪的早期迭代会「暂时」报错，故先入桶、用空内容继续；只有撑到收敛循环结束仍在的错误，才由驱动代码 `sink.delayed()` 提升为致命错误，从而过滤假错误并集中展示。
- `Traced` 跟踪某个 span 的取值，`get` 按 `FileId` 过滤以实现 comemo 缓存的精细化失效；被跟踪值收集在 `Sink.values`，供 `trace` API 返回。
- `Route` 是一条链表，用 `id` + `contains` 检测循环导入，用 `len` + `within` 检测过深嵌套；`upper` 字段以「上界剪枝 + CAS 收紧」兼顾深度判定与 comemo 缓存复用；四档 `MAX_*_DEPTH` 让不同类错误各得其所。
- `parallelize` 用「每任务一个子 `Sink`」破解 `TrackedMut` 的单线程可变借用约束，收尾时串行 `extend` 把四桶合并回外层 sink，警告复用去重、values 复用上限。

## 7. 下一步学习建议

- **u5-l3 诊断系统**：本讲反复出现 `SourceResult`、`SourceDiagnostic`、`bail!`。下一讲会讲清 `bail!`/`error!`/`warning!` 宏、`Hint`、`At` trait 与错误 span 的归属，让你彻底看懂 `Route::check_*` 里那些 `bail!(...; hint: ...)` 的构造方式。
- **u9-l1 / u9-l3 内省与收敛循环**：本讲的 `Engine::introspect`、`Sink.introspections`、延迟错误提升都服务于收敛机制。u9 会讲 `Location`/`Locator`/`query`/`Introspector` 与 `convergence.rs` 的 5 次迭代（`MAX_ITERS`）、「document did not converge」诊断，把本讲埋下的线索接上。
- **继续阅读源码**：想加深理解，建议精读 `crates/typst/src/lib.rs` 的 `compile_impl`（收敛循环全貌），以及 `crates/typst-realize/src/lib.rs` 中所有 `route.increase()`/`check_show_depth()`/`route.decrease()` 的成对出现处——那是 `Route` 深度检测最密集的实战现场。
