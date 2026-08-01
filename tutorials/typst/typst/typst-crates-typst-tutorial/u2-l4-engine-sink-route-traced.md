# 编译上下文 Engine / Sink / Route / Traced

## 1. 本讲目标

本讲聚焦 Typst 编译器里贯穿「求值」和「布局」全过程的**中央上下文对象 `Engine`**，以及它随身携带的四个「乘客」。

读完本讲，你应当能够：

- 说清 `Engine` 结构体的 6 个字段各自代表什么、为什么这样设计（特别是 `library` 为什么单独缓存、`introspector` 为什么用 `Protected` 包裹）。
- 理解 `Sink` 是一个「只增容器」：它如何收集告警（并按 `(span, message)` 去重）、延迟错误、内省记录、追踪值。
- 读懂 `Route` 如何用一条带「上界」的链表同时完成两件事——循环导入检测（`contains`）与过深嵌套检测（`within`），并理解 `upper` 上界为何是「故意不精确」的。
- 理解 `Traced` 如何按文件 id 过滤被追踪的 span，从而只让「真正包含该 span 的文件」的求值结果失效。

本讲是 u2-l1（`compile_impl` 主流程）的下钻：主流程里反复出现的 `Engine { ... }` 字面量到底在装什么、`subsink` 与主 `sink` 如何合并，全部在本讲揭开。

## 2. 前置知识

- **comemo 增量缓存**：Typst 用 comemo 库给纯函数做记忆化（memoization）。被 `#[comemo::track]` 标注的类型，其方法调用会被「观察」并参与缓存失效判定。`Tracked<T>` 是只读句柄，`TrackedMut<T>` 是可写句柄。`Engine` 的几乎所有字段都是这类「被追踪的句柄」，所以 `Engine` 本质是一捆「能让 comemo 感知变化」的引用。这一背景直接决定了本讲里 `track()` / `track_mut()` / `upper` 上界等设计。
- **稳定化循环**（见 u2-l1、u2-l2）：`compile_impl` 每轮迭代都会**新建**一个 `Engine`（首轮用 `EmptyIntrospector`），把临时 `subsink` 上的诊断在收敛那一轮才合并进主 `sink`。本讲会把这套「拆 sink → 合 sink」的机制讲透。
- **`SourceResult` 与 `SourceDiagnostic`**（见 u1-l3）：`Result<T, EcoVec<SourceDiagnostic>>`；诊断靠 `severity` 区分错误与告警，且是「一批」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | **本讲的核心文件**。定义 `Engine`、`Sink`、`Route`、`Traced` 四个类型的全部逻辑。 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | `compile_impl` 在稳定化循环里构造 `Engine`、用 `extend_from_sink` 合并子 sink、在末尾 `sink.delayed()` 提升延迟错误——这是看 `Engine`/`Sink` 数据流的最佳入口。 |
| [crates/typst-eval/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs) | `typst_eval::eval` 用 `Route::extend(route).with_id(id)` 进入模块、用 `route.contains(id)` 拦截循环导入——是看 `Route` 真实用法的入口。 |
| [crates/typst-eval/src/call.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/call.rs) | 函数调用时用 `Route::extend(route)` 推进一帧嵌套深度，展示 `Route` 的「栈」语义。 |

## 4. 核心概念与源码讲解

### 4.1 Engine —— 编译期的中央上下文

#### 4.1.1 概念说明

求值（eval）和布局（layout）在 Typst 里是两个阶段，但它们需要同一组「全局状态」：标准库、外部世界（`World`）、当前内省器、被追踪的 span、告警收集器、调用路线。如果把这些状态作为一堆零散参数到处传，函数签名会又长又脆。

`Engine` 的作用就是把这组状态**打包成一个结构体**，作为求值与布局各处的「环境」统一传递。你可以把它想象成「编译器走到任何一行代码时随身背的背包」。

关键点：背包里的绝大多数物品都是 **comemo 追踪句柄**（`Tracked`/`TrackedMut`）。这样当 `World`、`Sink`、`Route`、`Traced` 发生变化时，comemo 能据此让相关记忆化结果失效——`Engine` 因此既是「数据容器」又是「缓存参与方」。

#### 4.1.2 核心流程

`Engine` 的生命周期与稳定化循环的每一轮对齐：

```
compile_impl 每一轮迭代
  ├─ 新建 subsink = Sink::new()
  ├─ 构造 Engine { world, library, introspector, traced, sink: subsink.track_mut(), route }
  ├─ 把 Engine 传给 T::create（布局）/ eval（求值）
  │     └─ 内部还会继续克隆/拆分 Engine（见 4.1.3 的 parallelize）
  └─ 收敛那一轮：sink.extend_from_sink(subsink) 把子 sink 合并回主 sink
```

`Engine` 自身提供三个核心方法：

- `delay`：把一个 `SourceResult` 的错误「押后」——先存成延迟错误，不立刻致命（配合 u2-l1 末尾的提升逻辑）。
- `parallelize`：在多个任务间并行求值，每个任务一个独立子 `Sink`，最后统一合并。
- `introspect`：内省的唯一写入点（u2-l3 详讲）。

#### 4.1.3 源码精读

先看 `Engine` 的 6 个字段（[engine.rs:17-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L17-L36)）：

```rust
pub struct Engine<'a> {
    /// The compilation environment.
    pub world: Tracked<'a, dyn World + 'a>,
    pub library: &'a LazyHash<Library>,
    pub introspector: Protected<Tracked<'a, dyn Introspector + 'a>>,
    pub traced: Tracked<'a, Traced>,
    pub sink: TrackedMut<'a, Sink>,
    pub route: Route<'a>,
}
```

逐字段说明：

- `world`（[L20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L20)）：被追踪的 `World` 只读句柄。u1-l2 讲过 `world.track()` 把 `&dyn World` 转成 `Tracked<dyn World>`，这里就是它的归宿。
- `library`（[L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L26)）：标准库引用。注释点明：`world.library()` 理论上也能取，但因为**访问太频繁**，编译器提前把它取一次缓存到 `Engine`，避免反复走 comemo 的 tracked 调用开销。
- `introspector`（[L28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L28)）：内省器句柄，但被 `Protected` 包裹。`Protected` 是 typst-utils 提供的「禁止直接 `.0` 取用」的封装——想拿到内省器必须走 `.access(理由)` 并给出一段说明字符串（见下方 `introspect` 方法里的用法）。这样能强制开发者意识到「读内省器」是一件需要登记的事。
- `traced`（[L30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L30)）：可能正在被追踪的 span（给 `trace()` 用，详见 4.4）。
- `sink`（[L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L32)）：可写的 `Sink` 句柄，所有告警/延迟错误/内省/追踪值都从这儿推进去（4.2 详讲）。
- `route`（[L35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L35)）：当前调用路线，用于检测循环导入与过深嵌套（4.3 详讲）。

`Engine::delay`（[engine.rs:42-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L42-L50)）：成功就返回值；失败则把错误塞进 `sink.delayed_errors`，并返回 `T::default()`——注意它**不中断执行**，这正是 u2-l1「延迟错误」机制的引擎侧入口。

最能体现「克隆 Engine、拆 sink、再合并 sink」模式的是 `Engine::parallelize`（[engine.rs:53-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L53-L102)）。它的核心三段：

```rust
// 1) 把只读字段全部“抄”出去（world/introspector/traced/library 是 Copy 或共享句柄）
let Engine { world, introspector, traced, ref route, library, .. } = *self;

// 2) 每个并行任务建一个全新的 Sink，构造一个独立 Engine
work.into_par_iter().map(|value| {
    let mut sink = Sink::new();              // 每任务一个空 sink
    let mut engine = Engine {
        world, introspector, traced, library,
        sink: sink.track_mut(),              // 指向自己的 sink
        route: route.clone(),                // 路线 clone（见 4.3）
    };
    (f(&mut engine, value), sink)            // 任务结束带回它各自的 sink
}).collect_into_vec(&mut pairs);

// 3) 把每个子 sink 的四类产物统一合并回主 self.sink
for (_, sink) in &mut pairs {
    let sink = std::mem::take(sink);
    self.sink.extend(sink.introspections, sink.delayed, sink.warnings, sink.values);
}
```

第 2 步里 `route.clone()` 是关键：`Route` 内部含 `AtomicUsize`，`clone` 会重新建一个原子（见 4.3 的 `Clone` 实现），这样各任务的深度检查互不干扰。第 3 步的 `self.sink.extend(...)`（[engine.rs:238-253](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L238-L253)）就是子 sink 回流主 sink 的统一通道。

`Engine::introspect`（[engine.rs:109-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L109-L117)）展示了 `Protected` 的正确用法：用 `.access(...)` 拿到内省器算值，同时把这次内省记进 `sink`：

```rust
pub fn introspect<I: Introspect>(&mut self, introspection: I) -> I::Output {
    let introspector = *self.introspector.access("is okay since we're recording it");
    let output = introspection.introspect(self, introspector);
    self.sink.introspection(Introspection::new(introspection));
    output
}
```

#### 4.1.4 代码实践

**目标**：用「源码阅读」的方式，把 `Engine` 在一次求值里「被克隆 → 各自写入子 sink → 合并回主 sink」的数据流画出来。

**操作步骤**：

1. 打开 [engine.rs:53-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L53-L102)（`parallelize`），确认它先解构 `Engine` 取出只读字段，再为每个任务造新 `Sink`。
2. 打开 [typst/src/lib.rs:147-159](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L147-L159)，看 `compile_impl` 每轮循环如何把 `Engine` 建在临时 `subsink` 上，收敛后 `sink.extend_from_sink(subsink)` 合并。
3. 画出如下数据流图（文字版即可）：

```
        ┌──────── compile_impl 主 sink（贯穿全部迭代） ────────┐
        │                                                        │
   每轮迭代:  subsink ──track_mut──▶ Engine                     │
        │            ▲                       │                  │
        │            └── (T::create / eval 内部) parallelize     │
        │                          │           │   │            │
        │                  子sink_0  子sink_1  子sink_n         │
        │                          └────┬───────┘               │
        │                       extend(各自四类产物)             │
        │                               ▼                        │
        │                          subsink                       │
        │              收敛那轮: extend_from_sink                │
        └─────────────────────────────▼──────────────────────────┘
                                    main sink
```

**需要观察的现象**：注意「只读字段（world/library/introspector/traced）被多个子 Engine 共享，但 `Sink` 一定是每任务独有一份」，因为 `Sink` 是可写的累积容器。

**预期结果**：你能指出「合并发生在两个层级」——`parallelize` 内部各子 sink → 该轮 `subsink`；收敛轮的 `subsink` → 主 `sink`。

#### 4.1.5 小练习与答案

**练习 1**：`Engine.library` 的类型是 `&'a LazyHash<Library>`，为什么不像 `world` 那样也用 `Tracked` 句柄？

> **参考答案**：注释（[L22-L26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L22-L26)）说明 `library` 访问极其频繁，每次都走 `world.library()` 的 tracked 调用开销太大，所以提前取一次普通引用缓存起来。`Library` 本身用 `LazyHash` 包裹，其内容在一次编译内不变，不需要 comemo 的细粒度追踪。

**练习 2**：`Engine` 有 `route: Route<'a>` 而不是 `Tracked<Route>`，这意味着什么？

> **参考答案**：`Route` 不是直接以句柄形式存在 `Engine` 里，但它内部字段 `outer` 是 `Tracked<'a, Self, ...>`（[L265](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L265)），且 `Route` 自身 `#[comemo::track]`。所以路线仍参与缓存，只是封装形式不同——`Engine` 持有的是一个「当前段」结构体，向上通过 `outer` 句柄串成链。详见 4.3。

---

### 4.2 Sink —— 只增容器（告警去重 / 延迟错误 / 内省 / 追踪值）

#### 4.2.1 概念说明

编译过程中会产生四类「副产物」需要被收集：

1. **告警（warnings）**：不致命的问题（如未使用的变量）。
2. **延迟错误（delayed errors）**：可能在收敛后自动消失的错误（如 show rule 在前几轮因为内省器未就绪而报错），先押后判。
3. **内省记录（introspections）**：每一轮里实际做过的 `query`、计数器查询等，供收敛分析用（u2-l3）。
4. **追踪值（traced values）**：在被追踪 span 处观察到的 `(Value, Option<Styles>)`，供 `trace()` 用（u3-l5）。

`Sink` 就是这四类副产物的统一收集容器。它的核心约束写在类型注释里——它是一个 **push-only（只增）容器**：所有被追踪方法的签名都是 `(&mut self, ..) -> ()`。这种「只写不读」的特性让 comemo 原则上不需要对它做缓存校验（注释提到该优化尚未实现）。

#### 4.2.2 核心流程

```
求值/布局过程中:
  engine.sink.warn(diag)        ──▶  先算 (span,message) 的 128 位哈希
                                      哈希已存在? 丢弃 ; 否则存入 warnings
  engine.sink.delayed_error(e)  ──▶  直接 push 到 delayed
  engine.sink.introspection(i)  ──▶  push 到 introspections
  engine.sink.value(v, s)       ──▶  若 values.len() < 10 则 push
末尾 (compile_impl):
  sink.delayed()                ──▶  取出延迟错误，非空则升级为致命 Err
  sink.warnings()               ──▶  连同产物包进 Warned 返回
```

#### 4.2.3 源码精读

`Sink` 的字段（[engine.rs:151-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L151-L167)）：

```rust
pub struct Sink {
    introspections: EcoVec<Introspection>,
    delayed: EcoVec<SourceDiagnostic>,
    warnings: EcoVec<SourceDiagnostic>,
    warnings_set: FxHashSet<u128>,   // 仅用于告警去重
    values: EcoVec<(Value, Option<Styles>)>,
}
```

注意 `warnings_set`（[L164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L164)）不是「产物」，只是去重用的辅助集合。

**告警去重**是 `Sink` 最值得读的方法（[engine.rs:222-228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L222-L228)）：

```rust
pub fn warn(&mut self, warning: SourceDiagnostic) {
    let hash = typst_utils::hash128(&(&warning.span, &warning.message));
    if self.warnings_set.insert(hash) {   // insert 返回 false 表示已存在
        self.warnings.push(warning);
    }
}
```

去重键是 `(span, message)` 的 128 位哈希。这与 `compile_impl` 之外、`typst` crate 顶层的 `deduplicate` 函数（[typst/src/lib.rs:197-204](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L197-L204)）用的是**同一套**哈希键——后者在出错路径上再兜一层去重。

**延迟错误**只做简单 `push`/`extend`（[engine.rs:212-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L212-L219)），不去重——因为延迟错误最终只关心「末轮是否还在」，量小且语义不同。它由 `Engine::delay`（4.1）和循环末尾的提升逻辑驱动：

```rust
// compile_impl 末尾（lib.rs:187-191）
let delayed = sink.delayed();
if !delayed.is_empty() {
    return Err(delayed);
}
```

**追踪值**有上限保护（[engine.rs:231-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L231-L235)，常量 `MAX_VALUES = 10` 见 [L171](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L171)）：超过 10 条后静默丢弃，避免恶意/失控的递归把内存撑爆。

**子 sink 合并**有两个入口：

- `extend_from_sink`（[engine.rs:199-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L199-L201)）：把另一个 `Sink` 整体并入，用于 `compile_impl` 收敛轮。
- `extend`（[engine.rs:238-253](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L238-L253)）：底层实现，被 `parallelize` 直接调用。

注意 `extend` 里告警是逐条走 `self.warn(...)` 的（[L247-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L247-L249)）——也就是说**合并时还会再做一次去重**，而延迟错误/内省则直接 `extend`（不去重）。追踪值合并时同样尊重 `MAX_VALUES` 上限（[L250-L252](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L250-L252)）。

末尾的「取出」方法都是消耗式或清空式：`delayed`（[L184-L186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L184-L186)）用 `std::mem::take` 清空字段再返回；`warnings`/`values`（[L189-L196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L189-L196)）取走 `self` 所有权。

#### 4.2.4 代码实践

**目标**：用两个具体场景，体会「告警去重」与「延迟错误」的不同命运。

**操作步骤**：

1. **告警去重场景**：假设一份文档里同一处 `span`、同一条 message 的告警，可能因为某元素在稳定化循环中被布局两次而产生两次 `sink.warn`。请对照 [warn](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L222-L228) 与 [extend](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L247-L249)，推导用户最终看到几条。
2. **延迟错误场景**：假设某 show rule 在第 1 轮因内省器为空而失败（被 `Engine::delay` 押后），到第 3 轮内省稳定后成功。请对照 [delay](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L42-L50) 与 [compile_impl 末尾提升](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L191)，推导它会不会变成致命错误。

**需要观察的现象**：

- 场景 1 中，哪怕 `subsink` 合并回主 `sink` 时又 `warn` 一次，`warnings_set` 已有该哈希，第二条被丢弃。
- 场景 2 中，第 3 轮成功后，**该轮的 `subsink` 里根本没有这条延迟错误**（因为没再失败），所以收敛后合并进主 `sink` 的内容里不含它；末尾 `sink.delayed()` 取出为空，不升级。

**预期结果**：场景 1 → 用户只看到 1 条告警；场景 2 → 不报错，编译成功。

> 待本地验证：以上结论基于源码静态推导；如需确证，可在 `warn`/`delay` 处临时加日志观察真实运行路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `warnings` 要去重，而 `delayed` 和 `introspections` 不去重？

> **参考答案**：告警面向用户，同一处反复触发会刷屏，所以用 `(span,message)` 哈希去重。延迟错误在循环里量小且语义是「末轮是否仍在」，去重无意义；内省记录需要**保留每一次**观察供 `analyze` 还原历史（u2-l3），去重反而会破坏历史序列。

**练习 2**：`Sink` 派生了 `Clone`（[L151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L151)），但实际代码里从不 `clone` 它来「共享」，而是新建 `Sink::new()`。为什么？

> **参考答案**：因为 `Sink` 是可写的累积容器。如果两个子任务共享同一个（哪怕是 clone 的）`Sink`，由于各自通过 `track_mut` 拿到的是独立句柄，写入会落到各自副本、最后还得手动合并——所以索性各自从空开始，结束统一 `extend`。`Clone` 派生更多是给 comemo 内部或测试用。

---

### 4.3 Route —— 嵌套深度与循环导入检测

#### 4.3.1 概念说明

`Route` 记录「编译器是怎么走到当前位置的」——一条从根到当前的调用栈。它要解决两个问题：

1. **循环导入检测**：`a.typ` 里 `#include "b.typ"`，`b.typ` 又 `#include "a.typ"`，会无限递归。`Route` 记录栈上每个模块的 `FileId`，发现重复就拦截。
2. **过深嵌套检测**：show rule 匹配自己的输出、过深的函数调用/布局嵌套，会造成栈溢出。`Route` 记录嵌套深度，超过阈值就报错。

它是一条**链表**：每个 `Route` 是「当前段」，通过 `outer` 指向父段。

#### 4.3.2 核心流程

```
进入模块求值 (typst_eval::eval):
  先 route.contains(id) ?  循环 → panic
  再 Route::extend(route).with_id(id)  推进一帧，并记下模块 id

进入函数调用 / show rule / 嵌套布局:
  Route::extend(route)         推进一帧（不记 id），len 默认 +1

各处深度检查:
  route.check_show_depth()   超过 64 → "maximum show rule depth exceeded"
  route.check_layout_depth() 超过 72 → "maximum layout depth exceeded"
  route.check_call_depth()   超过 80 → "maximum function call depth exceeded"
```

#### 4.3.3 源码精读

`Route` 的字段（[engine.rs:258-281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L258-L281)）：

```rust
pub struct Route<'a> {
    outer: Option<Tracked<'a, Self, <Route<'static> as Track>::Call>>,
    id: Option<FileId>,
    len: usize,
    upper: AtomicUsize,
}
```

- `outer`（[L265](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L265)）：父段句柄；`None` 表示这是根段。
- `id`（[L268](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L268)）：仅当本段是「模块求值入口」时设为该模块 `FileId`。
- `len`（[L274](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L274)）：本段贡献的嵌套深度；**整条链的总深度 = 各段 `len` 之和**。
- `upper`（[L280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L280)）：父链长度的一个**上界**（详见后文）。

**推进与构造**：

- `Route::root()`（[L285-L292](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L285-L292)）：空路线，`len=0`、`upper=0`。
- `Route::extend(outer)`（[L295-L302](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L295-L302)）：压一帧，`len=1`、`upper=usize::MAX`（父链长度未知，先用最大值）。
- `with_id`/`unnested`（[L305-L312](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L305-L312)）：分别给段贴模块 id、或把本段 `len` 置 0（用于「记账但不计深度」的段）。

真实用法见 `typst_eval::eval`（[typst-eval/src/lib.rs:49-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L49-L63)）：

```rust
let id = source.id();
if route.contains(id) {                                  // 循环导入 → panic
    panic!("Tried to cyclicly evaluate {:?}", id.vpath());
}
...
let engine = Engine {
    ...
    route: Route::extend(route).with_id(id),             // 推进一帧并记模块 id
};
```

函数调用处则不记 id（[typst-eval/src/call.rs:666-673](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/call.rs#L666-L673)）：`Route::extend(route)`。

**循环导入检测** `contains`（[engine.rs:400-402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L400-L402)）：沿 `outer` 链查是否有段 `id` 等于目标。

**深度阈值**（[engine.rs:336-351](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L336-L351)）刻意取不同值：

| 检查 | 常量 | 阈值 |
|---|---|---|
| show rule | `MAX_SHOW_RULE_DEPTH` | 64 |
| layout | `MAX_LAYOUT_DEPTH` | 72 |
| html | `MAX_HTML_DEPTH` | 72 |
| function call | `MAX_CALL_DEPTH` | 80 |

注释（[L336-L339](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L336-L339)）解释了为何不同：当 show rule 检查与 call 检查交错时，**阈值越低优先级越高**——show rule 阈值最低（64），所以一旦发生「show 匹配自身输出」这种典型递归，会先触发 show rule 错误（带友好提示「maybe a show rule matches its own output」），而不是笼统的 call depth 错误。`check_show_depth` 等方法（[L354-L393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L354-L393)）都委托给 `within`。

**深度判定** `within`（[engine.rs:405-428](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L405-L428)）是 `Route` 最精巧的部分，依赖 `upper` 上界做短路：

```rust
pub fn within(&self, depth: usize) -> bool {
    let upper = self.upper.load(Relaxed);
    if upper.saturating_add(self.len) <= depth {      // ① 上界已知够小 → 直接 true
        return true;
    }
    match self.outer {
        Some(_) if depth < self.len => false,          // ② 本段已超
        Some(outer) => {
            let within = outer.within(depth - self.len);
            if within && depth < upper {               // ③ 把上界收紧到 depth
                self.upper.compare_exchange(upper, depth, Relaxed, Relaxed).ok();
            }
            within
        }
        None => true,                                  // 根段：必然 within
    }
}
```

**为什么用上界而非精确长度**——这是本模块的核心设计取舍，也是本讲综合实践题之一。源码注释（[L275-L280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L275-L280)）直说：「我们不知道确切长度（那会违背初衷，因为它会阻止不同、但未超深度的计算之间复用缓存）」。

理解要点：`Route` 是 `#[comemo::track]` 的，会作为记忆化函数的输入参与缓存键。如果 `Route` 把「确切深度」编码进去，那么在深度 3 算过的结果与在深度 5 算过的结果，对 comemo 就是**不同输入**，无法共享缓存——而这两者对「是否过深」的回答其实是一致的（都没超）。改用 `upper` 上界后：

- `upper` 只会**单调下降**（③ 用 `compare_exchange` 保证不会误增），它是「父链长度的一个合法上界」，用来回答 `upper + len ≤ depth?` 这个布尔问题绰绰有余。
- 上界比精确值「更粗」，comemo 看到的输入区分度更低，**更多不同深度的调用能命中同一缓存**——这正是「为缓存复用而故意不精确」。

#### 4.3.4 代码实践

**目标**：理解 `Route::within` 的上界短路，并解释它如何兼顾「正确性」与「缓存复用」。

**操作步骤**：

1. 读 [within](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L405-L428)，对照 `upper` 字段定义（[L280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L280)）。
2. 手动推演：构造一条链 `root(len=0, upper=0) → A(len=1, upper=MAX) → B(len=1, upper=MAX)`（即 `Route::extend` 两次）。分别求 `within(1)`、`within(2)`、`within(3)`，记录每次调用后各段 `upper` 的变化。

**需要观察的现象**：

- `within(3)`：`B.upper(64位MAX) + B.len(1) = 很大 > 3`，不短路；递归 `A.within(2)`；`A` 又递归 `root.within(1)` → 根段返回 true。回溯时 `A.upper` 收紧到 2，`B.upper` 收紧到 3。
- 再次对同一链 `within(3)`：`B.upper(3) + B.len(1) = 4 > 3`，仍不短路（注意 3+1=4 > 3），需递归；但 `A.upper(2)+A.len(1)=3 ≤ 3` → ①短路返回 true。
- `within(2)`：`B.upper + 1 ≤ 2`? 取决于上界当前值。

**预期结果**：你能解释「`upper` 越收越紧，命中 ① 短路的概率越高，从而减少递归」；并且能说出「若改成精确长度，深度 3 与深度 5 的同一段在 comemo 里会判为不同输入，缓存命中率下降」。

> 待本地验证：上界收紧是 `Relaxed` 原子操作，跨线程只保证原子性、不保证可见性顺序；在并行 `parallelize` 各克隆独立 `Route`（`Clone` 会复制 `upper`，[L437-L446](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L437-L446)），所以各任务的收紧互不污染——这恰好与 4.1 `parallelize` 里 `route.clone()` 呼应。

#### 4.3.5 小练习与答案

**练习 1**：`Route::track`（[L318-L323](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L318-L323)）在 `id.is_none() && len==0` 时直接返回 `outer`，跳过自身。这样省略一段会不会漏掉循环检测或深度判定？

> **参考答案**：不会。一个 `id=None` 且 `len=0` 的段**既不贡献模块 id（不影响 `contains`），又不贡献深度（不影响 `within`）**，所以省略它后，链的语义完全等价，却少了一层 comemo 追踪开销。这是一种「无意义段」折叠优化。

**练习 2**：`MAX_CALL_DEPTH(80) > MAX_SHOW_RULE_DEPTH(64)`，这对一个「show rule 调用了很深的函数」的用户意味着什么？

> **参考答案**：当递归同时逼近两种阈值时，show rule（64）先触发，用户看到的是带「maybe a show rule matches its own output」提示的 show rule 错误，而不是笼统的 call depth 错误——更指向真正的根因。这正是阈值分级的意图（[L336-L339](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L336-L339)）。

---

### 4.4 Traced —— 按文件 id 过滤的被追踪 span

#### 4.4.1 概念说明

`trace()` 入口（[typst/src/lib.rs:86-95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L86-L95)）让 IDE 实现「点击某表达式，查看它求值成了什么」。做法是：编译时盯住一个 span，把求值过程中**在这个 span 处**观察到的 `(Value, Styles)` 全收集进 `Sink.values`。

`Traced` 就是「装着这个被盯 span」的小结构。它的精妙之处不在「装」，而在 **`get(id)` 的过滤**：被追踪的 span 只属于某一个文件，求值别的文件时不应受它影响——否则 comemo 会因为「存在一个 traced span」而无谓地让无关文件的结果失效。

#### 4.4.2 核心流程

```
trace() 调用:
  Traced::new(span)         span 属于文件 file_b
  traced.track()            传入 compile_impl → 挂到每个 Engine.traced

求值文件 file_a:
  vm.engine.traced.get(file_a) → None     (span 不在 file_a)
  ⇒ comemo 视为「没有 traced」，file_a 的求值结果可正常缓存

求值文件 file_b:
  vm.engine.traced.get(file_b) → Some(span)
  ⇒ 命中追踪，把 span 处的值经 Sink::value 记录
```

#### 4.4.3 源码精读

`Traced` 本体极简（[engine.rs:120-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L120-L131)）：

```rust
#[derive(Default)]
pub struct Traced(Option<Span>);

impl Traced {
    pub fn new(traced: Span) -> Self { Self(Some(traced)) }
}
```

核心是 `get`（[engine.rs:133-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L133-L143)）：

```rust
#[comemo::track]
impl Traced {
    /// 仅当被追踪 span 属于给定文件时返回它，否则 None。
    /// 我们在 span 不属于该文件时把它藏起来，是为了只让
    /// 「含被追踪 span 的那个文件」的求值结果失效。
    pub fn get(&self, id: FileId) -> Option<Span> {
        if self.0.and_then(Span::id) == Some(id) { self.0 } else { None }
    }
}
```

注意 `get` 是被 `#[comemo::track]` 追踪的——也就是说，**对 comemo 而言 `traced.get(id)` 的返回值是缓存输入之一**。当 `id` 不匹配时返回 `None`，等价于「这个文件看不见被追踪 span」，于是该文件的求值不会被追踪行为污染，缓存照常命中。这是一个用「过滤」换取「缓存精度」的典型手法，与 `Route.upper` 的「故意不精确」异曲同工。

实际记录值的地方在求值器内部，最终汇入 `Sink::value`（4.2）；`trace()` 末尾用 `sink.values()`（[engine.rs:194-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L194-L196)）取出。`trace()` 与 `compile()` 共用 `compile_impl`，只是入口包了一个 `Traced::new(span)` 并在末尾用 `.ok()` 丢弃错误（[typst/src/lib.rs:91-94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L91-L94)）——trace 场景只关心 `values`，不关心编译是否成功。

#### 4.4.4 代码实践

**目标**：验证「文件 id 不匹配时 `get` 返回 `None`」，并推断它对缓存的影响。

**操作步骤**：

1. 读 [Traced::get](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L133-L143)，注意条件 `self.0.and_then(Span::id) == Some(id)`。
2. 假设：主文档 `main.typ` `#include "chapter.typ"`，被追踪的 span 位于 `chapter.typ`。问：求值 `main.typ` 顶层时 `traced.get(main_id)` 返回什么？求值 `chapter.typ` 时呢？

**需要观察的现象**：`main.typ` 求值得到 `None`，`chapter.typ` 求值得到 `Some(span)`。

**预期结果**：由于 `main.typ` 看到 `None`，comemo 不会因为「存在追踪」而把 `main.typ` 的缓存判为失效；只有真正包含 span 的 `chapter.typ` 才进入追踪记录路径。这避免了「追踪一个 span 就让整本文档所有文件重新求值」。

#### 4.4.5 小练习与答案

**练习 1**：`Traced` 用 `#[derive(Default)]`，`default()` 表示 `Option<Span>` 为 `None`。普通 `compile()`（非 `trace()`）里传入的是 `Traced::default().track()`（[typst/src/lib.rs:79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L79)）。此时 `get` 对任何文件返回什么？`Sink.values` 会有内容吗？

> **参考答案**：`None`（因为 `self.0` 是 `None`，`None.and_then(...)` 仍是 `None`，不等于 `Some(id)`）。既然没有任何 span 被命中，`Sink::value` 不会被调用，`values` 为空。这正符合普通编译「不追踪值」的预期。

**练习 2**：为什么 `Traced::get` 必须是被 comemo 追踪的方法（放在 `#[comemo::track]` 的 impl 块里），而不能是普通方法？

> **参考答案**：因为求值器 memoize 时会把 `traced.get(id)` 的返回值作为输入记录。只有它是被追踪的方法，comemo 才能在重放时观察它。返回 `None` 的文件被记录为「不依赖 traced」，下次缓存直接复用；返回 `Some(span)` 的文件则被正确关联，span 变化时才失效。普通方法对 comemo 是「黑盒」，无法参与这套精确失效。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿任务**（源码阅读 + 推导型，无需运行）。

### 任务：一次 `trace()` 调用里的 `Engine` 数据流与 `Route` 上界

设想 IDE 在 `main.typ` 的某个 span 上发起 `trace::<PagedDocument>(&world, span)`。请结合本讲源码，回答并画出：

1. **`Engine` 的诞生与拆合**：`compile_impl` 每轮迭代构造的 `Engine`（[lib.rs:147-154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L147-L154)）里，`traced` 字段来自哪里？求值期间若发生 `Engine::parallelize`（[engine.rs:53-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L53-L102)），各子任务的子 `Sink.values` 如何回流到主 `Sink`？画出包含「主 sink → subsink → 并行子 sink → 回流」的数据流图。

2. **`Route` 的双重职责**：求值进入 `#include "chapter.typ"` 时，`route.contains(id)`（[typst-eval/src/lib.rs:50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L50)）和 `Route::extend(route).with_id(id)`（[L62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L62)）分别承担什么职责？

3. **`upper` 上界的缓存意义**：用你自己的话解释，如果把 `Route.upper` 改成「父链的精确长度」，会对 comemo 的缓存命中率造成什么影响，并指明源码里哪句注释讲了这一点（[engine.rs:275-280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L275-L280)）。

### 参考答案要点

1. `traced` 来自 `trace()` 入口的 `Traced::new(span).track()`（[lib.rs:92-93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L92-L93)），一路透传进每轮 `Engine`。`parallelize` 内每个子任务各自 `Sink::new()`，命中 span 的子任务经 `Sink::value` 写入自己的 `values`；任务结束后 `self.sink.extend(...)`（[engine.rs:91-99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L91-L99)）把四类产物（含 `values`，受 `MAX_VALUES=10` 约束）合并；收敛轮再 `extend_from_sink` 进主 `sink`；`trace()` 末尾 `sink.values()`（[lib.rs:94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L94)）取出最终结果。
2. `contains(id)` 拦截循环导入（在 `eval` 入口 panic）；`Route::extend(route).with_id(id)` 压一帧、记下模块 id，使后续 `contains` 能发现重复，并让深度计数继续累计。
3. 精确长度会把「不同深度」编码成不同的 `Route` 输入，导致 comemo 把本可共享的结果判为不同缓存项；用 `upper` 上界（单调下降、仅够回答 `within` 的布尔问题）则输入更粗、缓存复用更高——这正是注释「it would prevent cache reuse of some computation at different, non-exceeding depths」的含义。

> 待本地验证：若想实测数据流，可在 `Sink::value`、`Engine::parallelize` 的合并处、`Route::within` 的 ① 短路处临时插入日志，观察一次 `trace()` 触发的写入与合并次数。

## 6. 本讲小结

- `Engine` 是贯穿求值与布局的**中央上下文**，6 个字段把 `World`/`Library`/`Introspector`/`Traced`/`Sink`/`Route` 捆在一起，绝大多数是 comemo 追踪句柄，使其同时是数据容器与缓存参与方。
- `Sink` 是**只增容器**，收集告警（按 `(span,message)` 128 位哈希去重）、延迟错误、内省记录、追踪值（上限 10）；子 sink 通过 `extend_from_sink`/`extend` 合并，合并时告警会再次去重。
- `Route` 是带 `outer` 父指针的链表，`contains` 检测循环导入、`within` 检测过深嵌套；四档阈值（64/72/72/80）刻意分级，让更具体的错误优先报出。
- `Route::within` 用单调下降的 `upper` 上界短路判定——**故意不精确**，换取 comemo 在不同深度间的缓存复用。
- `Traced::get(id)` 按文件 id 过滤 span，把追踪的影响限制在「真正含该 span 的文件」，避免污染其他文件的缓存。
- 「克隆 Engine → 各自子 Sink → 合并回流」是 `parallelize` 与稳定化循环共用的数据流范式。

## 7. 下一步学习建议

- 继续往**诊断处理**走：本讲的 `Sink` 去重、延迟错误提升、`Traced` 过滤，会在 **u3-l4（诊断处理：去重、延迟错误与友好提示）** 里和 `deduplicate`、`hint_invalid_main_file` 等汇成完整的诊断链路。
- 想看清 `Traced`/`Sink.values` 的最终用途，读 **u3-l5（trace() 值追踪机制）**，它会从 IDE「查看某处的值」场景把 `trace()` 与本讲的 `Traced::get`、`Sink::value` 串起来。
- 想理解 `Engine.introspector` 真正喂进去的内容，回到 **u2-l3（内省记录与非收敛检测）** 复习 `Engine::introspect` 与 `Sink.introspections` 如何被 `analyze` 消费。
- 建议顺带精读 [engine.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) 全文（不到 450 行），它是本讲四个类型的唯一权威来源；读完后可尝试自己画一张「一次 `compile()` 中 `Engine`/`Sink`/`Route` 的时序图」作为自查。
