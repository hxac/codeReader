# trace() 值追踪机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `typst` crate 里与 `compile()` 并列的第二个公开入口 `trace()` 的签名、返回值，以及它和 `compile()` 如何共用同一个 `compile_impl`。
- 解释 `Traced` 这个小结构体如何「携带一个目标 span」，以及它为什么在 `get(id)` 时要按文件 id 过滤。
- 读懂 `Sink::value` 如何在求值过程中把 `(Value, Option<Styles>)` 一条条记进只增容器，并理解 `MAX_VALUES = 10` 的上限。
- 描述求值侧（`Vm`）是如何把「正在被追踪的 span」与「每个表达式产出的值」对接起来，最终被 IDE 的「悬停查看表达式取值」功能消费。
- 能跟踪一条完整的调用链：IDE 点击表达式 → `analyze_expr` → `typst::trace` → `compile_impl` → 求值 → `sink.values()`。

本讲是专家层的最后一篇，依赖 u1-l3（`compile` 入口）和 u2-l4（`Engine`/`Sink`/`Route`/`Traced` 上下文）。我们不再重复 `compile` 主流程与内省循环的细节，而是聚焦「值追踪」这条与编译并列、却长期被忽略的副线。

## 2. 前置知识

阅读本讲前，请确认你已了解：

- **`compile()` 的薄外壳结构**：建 `Sink` → `world.track()` → 调 `compile_impl` → 去重 → 包成 `Warned` 返回（见 u1-l3）。
- **`compile_impl` 的七阶段**：取库 → 目标门控 → 装配样式 → 取主源码 → 求值 → 稳定化循环 → 提升延迟错误（见 u2-l1）。
- **`Engine` / `Sink` / `Traced` 的角色**：`Engine` 是贯穿求值与布局的中央上下文；`Sink` 是只增容器，收集告警、延迟错误、内省记录、追踪值；`Traced` 是「可能携带一个待观察 span」的乘客（见 u2-l4）。
- **comemo 的 `Track` / `Tracked`**：把普通值包装成可被增量缓存系统追踪的句柄；方法调用是否「命中缓存」取决于参数是否变化（见 u1-l2）。
- **`Span` 与 `FileId`**：`Span` 标记源码里的一个位置，并可通过 `Span::id()` 反查它所属的文件 id（`FileId`）。

一个关键直觉先建立起来：`compile()` 关心「文档产物是否正确」，而 `trace()` 只关心「在编译过程中，某个 span 处看到了哪些值」。两者跑的是同一套编译逻辑，区别只在于「要不要把某个 span 的值顺手抄一份下来」。理解了这一点，本讲剩下的内容都是在解释「这份抄录是怎么安排的」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/typst/src/lib.rs` | 定义 `trace()` 公开入口，并复用 `compile_impl`。本讲最核心的文件。 |
| `crates/typst-library/src/engine.rs` | 定义 `Traced`（携带 span）、`Sink`（记录值，含 `MAX_VALUES`）。 |
| `crates/typst-eval/src/vm.rs` | 虚拟机 `Vm`，在求值时用 `inspected` 字段对接 `Traced`，用 `trace_at`/`trace` 把值喂进 `Sink`。 |
| `crates/typst-eval/src/lib.rs` | `eval` 函数把 `traced` 一路透传到 `Vm`；`eval_string` 则用 `Traced::default()`（不追踪）。 |
| `crates/typst-eval/src/code.rs` | 大多数「值产生型」表达式在求值末尾统一调用 `trace_at`。 |
| `crates/typst-ide/src/analyze.rs` | `trace()` 在真实项目里的消费者：IDE 的 `analyze_expr` 悬停取值。 |

## 4. 核心概念与源码讲解

### 4.1 trace() 公开入口：与 compile 共用 compile_impl

#### 4.1.1 概念说明

`typst` crate 对外暴露两个公开编译入口：`compile()` 和 `trace()`。

- `compile()` 回答：「这份源码编译成什么样了？」产出文档（`PagedDocument` / `HtmlDocument`），并附上告警与错误。
- `trace()` 回答：「在编译过程中，这个 span 处到底取到了哪些值？」产出一个值列表，**不关心编译是否成功**。

这两件事看似不同，却共享同一套编译逻辑（解析 → 求值 → 布局 → …）。`typst` 的做法是：把真正的逻辑放进内部函数 `compile_impl`，让 `compile` 和 `trace` 都只是它的两个不同「调用姿势」。差别只有两处：

1. **传给 `compile_impl` 的 `Traced` 不同**：`compile` 传 `Traced::default()`（不追踪任何 span），`trace` 传 `Traced::new(span)`（追踪指定 span）。
2. **对返回结果的处理不同**：`compile` 要产物和错误（出错还要 `deduplicate` 去重）；`trace` 用 `.ok()` 直接丢掉错误，只从 `sink` 里取 `values()`。

#### 4.1.2 核心流程

`trace()` 的执行流程可以概括为：

```text
trace(world, span)
  ├─ 1. 新建一个空 Sink
  ├─ 2. 用 Traced::new(span) 把目标 span 包起来，再 .track() 得到可追踪句柄
  ├─ 3. 调用 compile_impl::<T>(world.track(), traced, &mut sink)
  │        └─ 与 compile 完全相同的编译流程：
  │             取库 → 目标门控 → 样式 → 取主源码 → eval → 稳定化循环 → 提升延迟错误
  │        └─ eval 期间，凡是被追踪 span 处产生的值，都会被抄进 sink
  ├─ 4. 对 compile_impl 的返回值调 .ok()，丢弃所有错误
  └─ 5. 返回 sink.values() —— 一个 EcoVec<(Value, Option<Styles>)>
```

注意第 4 步：`.ok()` 把 `SourceResult<T>`（即 `Result<T, EcoVec<…>>`）里的错误直接扔掉，只保留「编译跑过没有」这个事实。这是 `trace` 的一个**有意设计**——后面 4.1.4 会解释为什么。

#### 4.1.3 源码精读

先看 `trace` 本体。它和 `compile` 几乎是孪生兄弟，放在一起对比最清楚：

`compile`（用于对照）—— [crates/typst/src/lib.rs:73-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L73-L82)：建 `Sink`，传 `Traced::default().track()`（不追踪），出错时 `map_err(deduplicate)` 去重，最后把产物和告警包进 `Warned`。

`trace`—— [crates/typst/src/lib.rs:84-95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L84-L95)：这段代码做了下面几件事——

```rust
#[typst_macros::time]
pub fn trace<T>(world: &dyn World, span: Span) -> EcoVec<(Value, Option<Styles>)>
where
    T: Output,
{
    let mut sink = Sink::new();
    let traced = Traced::new(span);
    compile_impl::<T>(world.track(), traced.track(), &mut sink).ok();
    sink.values()
}
```

逐行说明：

- `let mut sink = Sink::new();` —— 新建一个独立的空 `Sink`，用来收集本次追踪到的值。
- `let traced = Traced::new(span);` —— 把用户传入的 `span` 包成 `Traced`（4.2 节详解）。
- `compile_impl::<T>(world.track(), traced.track(), &mut sink)` —— **这就是和 `compile` 共用的那一行**。`world.track()` 和 `traced.track()` 都是把值转成 comemo 可追踪句柄。`T: Output` 决定编译目标（IDE 里固定用 `PagedDocument`）。
- `.ok()` —— 把 `SourceResult<T>` 里的 `Err` 整个丢弃，只保留「跑完了」的信号。
- `sink.values()` —— 取出抄录下来的值列表返回。

再看 `compile_impl` 是如何「同时」服务于两者的—— [crates/typst/src/lib.rs:97-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L97-L131)。注意它的参数列表里第三个是 `traced: Tracked<Traced>`，它在求值时被原样透传给 `typst_eval::eval`：

```rust
let content = typst_eval::eval(
    world,
    library,
    traced,          // ← 透传给求值器
    sink.track_mut(),
    Route::default().track(),
    &main,
)?
```

也就是说，`compile_impl` 本身对「是不是在追踪」一无所知，它只是机械地把 `traced` 传下去。是 `Traced::default()` 还是 `Traced::new(span)`，完全由调用方（`compile` 或 `trace`）决定。这是「一份逻辑、两种姿势」的关键。

#### 4.1.4 代码实践

**实践目标**：用源码对照的方式，确认 `compile` 与 `trace` 共用 `compile_impl`，且只有 `Traced` 参数不同。

**操作步骤**：

1. 打开 [crates/typst/src/lib.rs:73-95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L73-L95)，把 `compile` 和 `trace` 两个函数并排看。
2. 列出两者的差异点（建议在纸上写）：传入 `compile_impl` 的 `Traced`、对返回值的处理、最终返回类型。
3. 思考：如果把 `trace` 第 93 行的 `.ok()` 换成 `?`，会发生什么？（提示：`trace` 的返回类型不是 `Result`，无法用 `?`；这正是它必须用 `.ok()` 的语法原因之一。）

**需要观察的现象 / 预期结果**：你会发现两者调用 `compile_impl` 的那一行几乎逐字相同，差别只在 `Traced::default().track()` vs `Traced::new(span).track()`，以及返回前的处理。这印证了「共用主流程」的设计。

> 「待本地验证」：若你想在本地实际跑一次 `trace`，需要先实现一个 `World`（参考 typst-cli 或 typst-ide 的测试 World），这超出本实践范围。本实践定位为「源码阅读型」。

#### 4.1.5 小练习与答案

**练习 1**：`trace` 函数体里调用了 `.ok()` 却没有使用它的返回值，这是为什么？去掉 `.ok()` 直接写 `compile_impl::<T>(...);` 行不行？

> **答案**：`compile_impl` 返回 `SourceResult<T>`（一个 `Result`）。`trace` 只关心「跑完」这个副作用（值已经抄进 sink），完全不关心成功还是失败。`.ok()` 的作用是显式地把 `Result` 拆成 `Option` 并丢弃错误信息，表达「我知道这里可能出错，但我有意忽略」。直接写 `compile_impl::<T>(...);`（忽略 `Result`）在 Rust 里会触发 `unused_must_use` 警告，因为 `Result` 标注了 `#[must_use]`；`.ok()` 是消除该警告、并表明意图的惯用写法。

**练习 2**：为什么 `trace` 也带泛型 `T: Output`，而不是固定产出 `PagedDocument`？

> **答案**：因为 `compile_impl` 是泛型的，它的目标门控、样式注入、布局调度都依赖 `T::target()` 与 `T::create()`（见 u3-l1）。`trace` 复用 `compile_impl`，就必须同样提供 `T`。在 IDE 实际调用里（4.4 节），`T` 固定写成 `PagedDocument`。

---

### 4.2 Traced：携带目标 span，并按文件 id 过滤

#### 4.2.1 概念说明

`Traced` 是一个极小的结构体，它只做一件事：**记住「这次编译要观察哪个 span」**。

- 在 `compile` 场景，它装的是 `None`（`Traced::default()`），表示「什么都不观察」。
- 在 `trace` 场景，它装的是 `Some(span)`，表示「请把 `span` 处出现的值都记下来」。

但 `Traced` 真正精妙的地方不在「装 span」，而在它的读取方法 `get(id)` 会**按文件 id 过滤**：只有当目标 span 恰好落在给定的 `id` 这个文件里时，才返回这个 span；否则返回 `None`。这个过滤不是多余的礼貌，而是 comemo 增量缓存的正确性所必需的。

#### 4.2.2 核心流程

```text
Traced(Some(span))                      ← 由 trace() 用 Traced::new(span) 构造
   │
   │  求值器对每个模块文件 id 调用 traced.get(id)
   ├─ 若 span 的文件 id == id   → 返回 Some(span)   → 该模块进入「追踪模式」
   └─ 若 span 的文件 id != id   → 返回 None         → 该模块「当作没在追踪」
```

为什么需要按文件过滤？因为 comemo 的缓存以「函数参数」为键。`eval` 是 `#[comemo::memoize]` 的，它的参数之一就是 `traced: Tracked<Traced>`。如果对**所有**模块都返回同一个 `Some(span)`，那么只要追踪目标 span 在文件 A，文件 B 的求值结果也会因为 `traced` 这一参数「看起来变了」而被判定为缓存失效——尽管文件 B 根本不可能产出这个 span 的值。按文件 id 过滤后，对文件 B 来说 `traced.get(B)` 恒为 `None`，与「不追踪」时一致，于是文件 B 的缓存得以保留；只有真正含有目标 span 的文件 A 才会失效缓存。

#### 4.2.3 源码精读

`Traced` 的定义与构造—— [crates/typst-library/src/engine.rs:120-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L120-L131)：

```rust
/// May hold a span that is currently under inspection.
#[derive(Default)]
pub struct Traced(Option<Span>);

impl Traced {
    /// Wraps a to-be-traced `Span`.
    /// Call `Traced::default()` to trace nothing.
    pub fn new(traced: Span) -> Self {
        Self(Some(traced))
    }
}
```

`#[derive(Default)]` 让 `Option<Span>` 默认为 `None`，于是 `Traced::default()` 就是「不追踪」；`Traced::new(span)` 才是「追踪指定 span」。这就是 `compile` 用 `default()`、`trace` 用 `new(span)` 的全部来源。

关键的过滤读取方法—— [crates/typst-library/src/engine.rs:133-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L133-L143)：

```rust
#[comemo::track]
impl Traced {
    /// Returns the traced span _if_ it is part of the given source file or
    /// `None` otherwise.
    ///
    /// We hide the span if it isn't in the given file so that only results for
    /// the file with the traced span are invalidated.
    pub fn get(&self, id: FileId) -> Option<Span> {
        if self.0.and_then(Span::id) == Some(id) { self.0 } else { None }
    }
}
```

要点：

- `#[comemo::track]` 让 `get` 成为可被缓存追踪的方法——它的返回值会被 comemo 记录，并在参数（`self` 的内容、`id`）不变时直接复用。
- `self.0.and_then(Span::id)` 取出待追踪 span 的文件 id；若它等于传入的 `id`，返回 `self.0`（即 `Some(span)`），否则返回 `None`。
- 注释「only results for the file with the traced span are invalidated」点明了设计意图：把缓存失效范围**收敛到唯一一个文件**。

#### 4.2.4 代码实践

**实践目标**：用一组具体场景，验证 `Traced::get` 的文件过滤行为对缓存的影响。

**操作步骤**：

1. 假设有两个文件：`main.typ`（含被点击的表达式，span 的文件 id = `main`）和 `lib.typ`（被 `main.typ` import，不含被点击表达式）。
2. 设 `span` 落在 `main.typ`，于是 `Traced::new(span)` 内部 `Some(span)`，且 `Span::id(span) == Some(main)`。
3. 推演两次调用的返回：
   - 求值 `main.typ` 时 `traced.get(main)` → `Some(span)` → 进入追踪模式。
   - 求值 `lib.typ` 时 `traced.get(lib)` → `None` → 不追踪。

**预期结果**：你会得出结论——只有 `main.typ` 这一份求值会因为「正在追踪」而与普通编译不同；`lib.typ` 完全感知不到追踪的存在，其 comemo 缓存照常命中。这正是按文件过滤的价值。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `get` 里的文件过滤，直接返回 `self.0`（即对任何文件都返回 `Some(span)`），功能上「追踪」还能工作吗？会带来什么副作用？

> **答案**：功能上仍能追踪到值（毕竟 span 还是返回了）。但副作用是：每次开启追踪，**所有**被求值的模块都会因为 `traced` 这一参数变化而缓存失效，哪怕它们根本不包含目标 span。在 IDE 场景里（用户每移动一下光标就可能触发一次追踪），这会让增量编译退化为接近全量重算，严重拖慢响应。文件过滤把失效范围限制在一个文件内，是性能上的关键优化。

**练习 2**：`Span::id(span)` 可能返回什么？为什么用 `and_then`？

> **答案**：`Span::id` 返回 `Option<FileId>`——detached span（没有绑定到具体文件的 span）返回 `None`，正常源码 span 返回 `Some(id)`。用 `and_then(Span::id)` 是因为 `self.0` 本身是 `Option<Span>`：先取出 span（若有的话），再取它的文件 id（若有的话），两层 `Option` 用 `and_then` 串接。如果待追踪 span 是 detached 的，`get` 对任何 `id` 都返回 `None`，等价于「不追踪」。

---

### 4.3 Sink::value 与 MAX_VALUES：只增容器如何记录值

#### 4.3.1 概念说明

`Sink` 是 typst 编译器里那个「只增容器」（见 u2-l4）：它收集告警、延迟错误、内省记录，以及本讲关心的**追踪值**。追踪值就存放在 `Sink` 的一个字段里：

```rust
values: EcoVec<(Value, Option<Styles>)>
```

每条记录是一个二元组：一个 `Value`（表达式求出的值）和一个 `Option<Styles>`（求值那一刻生效的样式，可能没有）。`trace()` 最后返回的就是这个 `EcoVec`。

为什么一个 span 可能对应**多个**值？因为同一个表达式可能在循环里被求值多次（例如 `for` 循环体里的表达式每轮迭代都求一次值，每次都带着不同的上下文样式），也可能在递归或不同调用点被命中多次。所以 `values` 是一个列表，而不是单个值。

为了防止失控（比如一个死循环表达式被求值成千上万次），`Sink` 给追踪值设了硬上限 `MAX_VALUES = 10`：超过 10 条就静默丢弃后续记录。

#### 4.3.2 核心流程

```text
求值时 Vm::trace(value) 被调用
   │
   ▼
Sink::value(value, styles):
   ├─ if self.values.len() < MAX_VALUES(=10):
   │      self.values.push((value, styles))   ← 记录
   └─ else:
          什么都不做                            ← 静默丢弃（已达上限）

（稳定化循环/并行任务合并子 sink 时）
Sink::extend(...): 同样尊重 MAX_VALUES 上限，按剩余配额 take
```

「只增」体现在：所有 tracked 方法都是 `(&mut self, ..) -> ()`，只往里塞东西，不删不改。`trace()` 结束后用 `sink.values()`（按值消费，`std::mem::take` 风格）把列表取出来。

#### 4.3.3 源码精读

先看 `Sink` 的字段定义，重点关注 `values`—— [crates/typst-library/src/engine.rs:151-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L151-L167)。其中 `values: EcoVec<(Value, Option<Styles>)>` 就是追踪值的存放处。

上限常量与读取方法—— [crates/typst-library/src/engine.rs:169-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L169-L196)：

```rust
impl Sink {
    /// The maximum number of traced values.
    pub const MAX_VALUES: usize = 10;
    ...
    /// Get the values for the traced span.
    pub fn values(self) -> EcoVec<(Value, Option<Styles>)> {
        self.values
    }
    ...
}
```

`MAX_VALUES = 10` 是个 `pub const`，外部可见。`values(self)` 按 `self` 消费（注意它接收 `self` 而非 `&self`），取出内部 `values` 字段返回——这正是 `trace()` 最后一行 `sink.values()` 能拿到列表的原因。

写入逻辑（tracked 方法）—— [crates/typst-library/src/engine.rs:230-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L230-L235)：

```rust
/// Trace a value and optionally styles for the traced span.
pub fn value(&mut self, value: Value, styles: Option<Styles>) {
    if self.values.len() < Self::MAX_VALUES {
        self.values.push((value, styles));
    }
}
```

简洁明了：未达 10 条就 push，已达上限就什么都不做（**静默丢弃**，不报错也不告警）。

合并子 sink 时同样尊重上限—— [crates/typst-library/src/engine.rs:237-253](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L237-L253) 的 `extend` 方法末尾：

```rust
if let Some(remaining) = Self::MAX_VALUES.checked_sub(self.values.len()) {
    self.values.extend(values.into_iter().take(remaining));
}
```

这处理的是稳定化循环（每轮一个临时 `subsink`）和并行任务（`Engine::parallelize` 里每个任务一个子 sink）把结果并回主 sink 的情形：先算出「还能装几条」，再 `take(remaining)` 只搬这么多。两处上限保护，确保无论求值在多少个子任务里展开，`values` 总量都不会超过 10。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码，理解「一个 span 可能产生多条值」以及上限的兜底作用。

**操作步骤**：

1. 想象一段 Typst 代码 `#for i in (1,2,3) { i * 2 }`，假设用户把光标悬停在表达式 `i * 2` 上。
2. 推演：循环 3 轮，`i * 2` 这个 span 被求值 3 次，分别得到 `2`、`4`、`6`，于是 `Sink::value` 会被调用 3 次。
3. 回答：最终 `sink.values()` 返回几条？每条的 `Value` 是什么？

**预期结果**：返回 3 条：`[(2, styles_0), (4, styles_1), (6, styles_2)]`（`styles_x` 取决于每轮迭代时的上下文样式，可能都是 `None`）。IDE 据此能在悬停时展示「这个表达式可能取到 2 / 4 / 6」。

**延伸思考（待本地验证）**：如果把循环改成 20 轮，`i * 2` 会被求值 20 次，但 `MAX_VALUES = 10`，所以最终只保留前 10 条。这是为了避免恶意或失控的文档拖垮 IDE。

#### 4.3.5 小练习与答案

**练习 1**：`values(self)` 为什么接收 `self`（按值）而不是 `&self`？

> **答案**：因为它在 `trace()` 里是「最后一次取用」——`trace` 取完值就结束了，`Sink` 不再需要。按值接收（实际是移动出 `values` 字段）避免了一次 clone（`EcoVec` 的克隆不便宜）。同时这也呼应 `Sink` 是「一次性消费」的设计：告警用 `warnings(self)`、延迟错误用 `delayed(&mut self)`（内部 `std::mem::take`），都是消费语义。

**练习 2**：为什么 `extend` 里用 `checked_sub` 而不是直接 `MAX_VALUES - self.values.len()`？

> **答案**：若 `self.values.len()` 已经超过 `MAX_VALUES`（理论上不会，因为 `value` 有保护，但防御性编程），直接相减会下溢 panic。`checked_sub` 在下溢时返回 `None`，`if let Some(remaining)` 自然跳过搬运，是安全的写法。

---

### 4.4 求值侧的接线：Vm 如何把值喂进 Sink，以及 IDE 的真实调用

#### 4.4.1 概念说明

到目前为止，我们知道 `Sink::value` 负责记录、`Traced::get` 负责按文件过滤。但还有一环没讲：**求值器在求每一个表达式时，怎么知道「现在该不该记录」「记录哪个值」？**

答案在虚拟机 `Vm` 里。`Vm` 有一个字段 `inspected: Option<Span>`：

- 创建 `Vm` 时，用 `engine.traced.get(当前模块的文件 id)` 初始化它。
- 如果当前模块正是「含目标 span 的那个文件」，`inspected = Some(目标 span)`，这台 `Vm` 进入「追踪模式」。
- 否则 `inspected = None`，这台 `Vm` 与普通求值无异。

在追踪模式下，`Vm` 对每一个「值产生型」表达式求完值后，都会调用 `trace_at(span, &value)`：只有当这个表达式的 span **正好等于** `inspected`（即目标 span）时，才把值通过 `Vm::trace` 喂进 `engine.sink.value(...)`。

这套机制的真正消费者是 typst-ide crate：当用户在编辑器里把鼠标悬停到某个表达式上，IDE 调用 `analyze_expr`，后者调用 `typst::trace::<PagedDocument>(world, node.span())`，拿到该 span 处所有可能的值，用于显示悬停提示（hover/tooltip）。

#### 4.4.2 核心流程

```text
IDE：用户悬停表达式 E（语法节点 node）
   │
   ▼
typst_ide::analyze_expr(world, node)
   ├─ 对字面量（None/Bool/Int/Str...）：直接构造 Value，返回 [(val, None)]
   └─ 对其它表达式：
         typst::trace::<PagedDocument>(world.upcast(), node.span())
            │
            ▼
         trace() → compile_impl() → typst_eval::eval(main)
            │  （traced 一路透传到 Engine，再到 Vm）
            ▼
         Vm::new(...) 中：inspected = engine.traced.get(当前文件 id)
            │  = Some(node.span())   （因为 node 就在这个文件里）
            ▼
         求值每个表达式后调 Vm::trace_at(span, &value):
            ├─ if inspected == Some(span):   ← 命中目标表达式
            │     Vm::trace(value) → engine.sink.value(value, styles)
            └─ else: 跳过
            │
            ▼
         trace() 返回 sink.values() → 回到 IDE，渲染成悬停提示
```

一个重要细节：求值阶段（`typst_eval::eval`）使用的是真实 `traced`，所以会记录值；但**布局阶段**通过 `ROUTINES` 间接调用的 `eval_string`/`eval_closure`（用于求 context 表达式）内部用的是 `Traced::default()`（不追踪），所以那些上下文重求值**不会**贡献追踪值。追踪值只来自主模块的那一次 `eval`。

#### 4.4.3 源码精读

先看 `Vm` 的字段，重点关注 `inspected`—— [crates/typst-eval/src/vm.rs:16-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/vm.rs#L16-L28)。注释明确写道：「If this is `Some`, we're in tracing mode, and will record every value the given span sees.」

`Vm::new` 如何用 `traced.get` 初始化 `inspected`—— [crates/typst-eval/src/vm.rs:31-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/vm.rs#L31-L40)：

```rust
pub fn new(
    engine: Engine<'a>,
    context: Tracked<'a, Context<'a>>,
    scopes: Scopes<'a>,
    target: Span,
) -> Self {
    let inspected = target.id().and_then(|id| engine.traced.get(id));
    Self { engine, context, flow: None, scopes, inspected }
}
```

`target` 是当前模块根节点的 span，`target.id()` 取出当前模块的文件 id；`engine.traced.get(id)` 正是 4.2 节那个按文件过滤的方法。于是：只有当待追踪 span 在当前模块时，`inspected` 才是 `Some`。

两个核心方法—— [crates/typst-eval/src/vm.rs:75-91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/vm.rs#L75-L91)：

```rust
/// Helper to only call [`Self::trace`] for a value if we're inspecting its
/// span. This method (or `trace`) should be called for every value produced
/// by an expression.
pub fn trace_at(&mut self, span: Span, value: &Value) {
    if self.inspected == Some(span) {
        self.trace(value.clone());
    }
}

/// Trace a value. Tracing powers IDE tooltips and hover info. This method
/// should be called for every value produced by an expression.
#[cold]
pub fn trace(&mut self, value: Value) {
    self.engine
        .sink
        .value(value, self.context.styles().ok().map(|s| s.to_map()));
}
```

要点：

- `trace_at` 是「带条件」的入口：只有 `inspected == Some(span)`（当前表达式的 span 正是目标 span）才继续。这保证了即便整台 `Vm` 在追踪模式下，也只会记录**目标表达式那一个 span** 的值，而不会把求值路径上所有表达式的值都记下来。
- `trace` 是真正写 sink 的地方，调用 `engine.sink.value(value, styles)`（4.3 节）。样式取自 `self.context.styles()`，失败时为 `None`。
- `#[cold]` 提示编译器：这条路径（追踪）在普通编译里几乎不走，优化时可以把它挪到一边，不让它拖累热路径。这是一个为「编译性能不受追踪机制拖累」的微优化。

那 `trace_at` 在哪里被调用？几乎每个值产生型表达式求值末尾都会调一次。最集中的地方是 `Expr::eval`—— [crates/typst-eval/src/code.rs:148-154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/code.rs#L148-L154)：

```rust
        }?
        .spanned(span);

        // This satisfies the obligation to call `Vm::trace` for almost all
        // value-producing expressions!
        vm.trace_at(span, &value);

        Ok(value)
```

注释点明了约定：「几乎每个值产生型表达式」都在求值末尾调用 `trace_at`。少数特殊情况（如函数调用的 callee、数学表达式的值、import 的模块值）因为没有走这条统一路径，会在各自的位置手动补一次 `trace_at`（例如 `call.rs`、`math.rs`）。

最后，看真实消费者 IDE—— [crates/typst-ide/src/analyze.rs:12-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-ide/src/analyze.rs#L12-L50)。函数 `analyze_expr(world, node)` 对字面量直接构造值返回，对其他表达式则落到这一行（[analyze.rs:45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-ide/src/analyze.rs#L45)）：

```rust
return typst::trace::<PagedDocument>(world.upcast(), node.span());
```

`world.upcast()` 把 `&dyn IdeWorld` 上转成 `&dyn World`（`IdeWorld: World` 是子 trait，见 [typst-ide/src/lib.rs:25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-ide/src/lib.rs#L25)），`node.span()` 是被悬停语法节点的 span。`trace` 返回的 `EcoVec<(Value, Option<Styles>)>` 直接成为悬停提示的数据来源。配套的 `analyze_expr_with_fallback`（[analyze.rs:56-77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-ide/src/analyze.rs#L56-L77)）在 `trace` 拿不到值时（例如死代码里的标识符）退回标准库定义，注释称之为「best-effort results in dead code」——这正是 4.1.4 里 `.ok()` 丢弃错误的用意：即便编译在别处失败，`trace` 仍尽力返回它能观察到的值。

#### 4.4.4 代码实践

**实践目标**：完整跟踪「IDE 点击表达式查看其求值结果」时 `trace()` 的调用链，把本讲四个模块串起来。

**操作步骤**：

1. 假设用户在编辑器里把光标悬停在 `main.typ` 中的表达式 `1 + 2` 上。编辑器定位到语法节点 `node`，其 `node.span()` 记为 `S`（文件 id = `main`）。
2. 按顺序回答下列问题（参考前面各节的源码）：
   - **入口**：IDE 调用哪个函数？传入了什么 span？（答：`typst::trace::<PagedDocument>(world.upcast(), S)`。）
   - **构造追踪态**：`trace` 内部用 `S` 构造了什么？它如何被 `compile_impl` 接收？（答：`Traced::new(S)`，再 `.track()`，作为 `compile_impl` 的 `traced` 参数透传到 `eval`。）
   - **求值侧对接**：求值 `main.typ` 时，`Vm::new` 里 `inspected` 取到了什么？求值 `lib.typ`（被 import 的文件）时呢？（答：`main` 的 `Vm` 得到 `Some(S)`；`lib` 的 `Vm` 得到 `None`。）
   - **记录**：当 `1 + 2` 这个 span 被求值为 `3` 时，发生了什么？（答：`trace_at(S, &3)` 命中 `inspected == Some(S)`，调用 `Vm::trace(3)` → `engine.sink.value(3, styles)`。）
   - **返回**：`trace` 最后如何把结果交回 IDE？（答：丢弃错误（`.ok()`），返回 `sink.values()`，即 `[(3, Some(styles))]`。）
3. 在纸上画出这条链路图（IDE → `trace` → `compile_impl` → `eval` → `Vm` → `Sink::value` → `values()` → IDE）。

**预期结果**：你能复述整条链路上每一步「传了什么、记了什么、过滤了什么」，并解释为什么 IDE 能在悬停时看到 `3`。

> 「待本地验证」：完整复现需要 IDE World 与编辑器集成。本实践定位为「源码阅读 + 链路推演」。若想接近真实运行，可阅读 `crates/typst-ide/src/tests.rs`，里面有构造 `IdeWorld` 并调用 `analyze_expr` 的测试用例，可作为运行态参考。

#### 4.4.5 小练习与答案

**练习 1**：`trace_at` 里的条件是 `self.inspected == Some(span)`。如果改成「只要 `inspected` 是 `Some` 就记录」，会有什么问题？

> **答案**：那样会把当前模块里**所有**表达式的值都记下来，而不是只记目标 span。`Sink::value` 会迅速被无关值填满（最多 10 条就被截断），真正关心的目标表达式的值反而可能被挤掉。精确比较 `== Some(span)` 才能保证只抄录用户真正点击的那一个表达式的值。

**练习 2**：为什么 `Vm::trace` 标注了 `#[cold]`？

> **答案**：`#[cold]` 告诉编译器这条分支在正常编译（`compile`，`Traced::default()`，`inspected` 恒为 `None`）里几乎从不执行——`trace_at` 的条件永远不成立，`trace` 自然不会被调到。把冷路径分离出去，可以让编译器更激进地优化求值热路径，确保「追踪机制的存在不拖慢普通编译」。这是 typst 把 IDE 友好性与编译性能兼顾的一个细节。

**练习 3**：稳定化循环里，布局阶段也会求值一些表达式（通过 `eval_string`）。它们产生的值会被记进追踪 sink 吗？

> **答案**：不会。布局阶段用的 `eval_string` 内部构造了 `Traced::default()`（[typst-eval/src/lib.rs:140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L140)），即「不追踪」，所以那些上下文重求值的 `Vm` 的 `inspected` 恒为 `None`，不会记录。追踪值只来自主模块的那一次 `eval`。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串成一条完整的「值追踪」数据流说明，并指出其中三处「刻意设计」及其动机。

请撰写一段技术说明（文字即可，不必运行），覆盖以下要点：

1. **入口对照**：用一句话说明 `trace(world, span)` 与 `compile(world)` 在调用 `compile_impl` 时的唯一实质差异。
2. **span 的旅程**：从用户传入的 `span` 开始，依次写出它如何被 `Traced::new` 包装、如何被 `traced.get(id)` 按文件过滤、如何成为 `Vm::inspected`、最终如何与 `trace_at(span, value)` 里的 span 比较。
3. **值的旅程**：从 `Vm::trace` 写入 `engine.sink.value(...)`，到稳定化循环/并行子任务合并时如何尊重 `MAX_VALUES`，最后到 `sink.values()` 被 `trace()` 返回、被 IDE `analyze_expr` 消费。
4. **三处刻意设计**：至少指出并解释以下三处设计意图——
   - `trace` 用 `.ok()` 丢弃错误（动机：best-effort，即便别处出错也要尽力返回观察到的值）。
   - `Traced::get` 按文件 id 过滤（动机：把 comemo 缓存失效收敛到唯一一个文件）。
   - `MAX_VALUES = 10` 与静默丢弃（动机：防止失控文档拖垮 IDE）。

完成后，对照本讲 4.1–4.4 的源码引用自查：你是否每一条结论都能指到具体文件和行号？凡指不到的，标注「待确认」而不是想当然。

## 6. 本讲小结

- `trace()` 是 `typst` crate 与 `compile()` 并列的第二个公开入口，两者**共用 `compile_impl`**，唯一实质差异是传入的 `Traced`：`compile` 用 `Traced::default()`（不追踪），`trace` 用 `Traced::new(span)`（追踪指定 span）。
- `trace` 对 `compile_impl` 的返回值调 `.ok()` **丢弃错误**，最后返回 `sink.values()`——它只关心「观察到了哪些值」，不关心编译是否成功，从而支持 IDE 在死代码/带错文档里也能 best-effort 地给出悬停取值。
- `Traced` 用 `get(id)` **按文件 id 过滤**：只有含目标 span 的那个文件才返回 `Some(span)`，其余文件返回 `None`，把 comemo 缓存失效范围收敛到唯一一个文件。
- `Sink::value` 是只增容器里记录追踪值的方法，存的是 `(Value, Option<Styles>)`；硬上限 `MAX_VALUES = 10` 在 `value` 和 `extend` 两处共同生效，超限静默丢弃，防失控。
- 求值侧由 `Vm` 接线：`Vm::new` 用 `traced.get(id)` 初始化 `inspected`；每个值产生型表达式求值后调 `trace_at(span, value)`，仅当 `inspected == Some(span)`（精确命中目标表达式）才经 `Vm::trace`（`#[cold]`）写入 `engine.sink`。
- 真实消费者是 typst-ide 的 `analyze_expr`：用户悬停表达式时，它调用 `typst::trace::<PagedDocument>(world, node.span())`，把返回的值列表渲染成悬停提示。

## 7. 下一步学习建议

- **回看 u2-l4**：本讲的 `Sink`、`Traced`、`Engine` 全部定义在 `engine.rs`，u2-l4 给出了它们作为「中央上下文乘客」的全景。若对 `extend_from_sink` / `parallelize` 里子 sink 合并回流仍有疑问，那是最佳的补充阅读。
- **阅读 `crates/typst-ide`**：本讲只点了 `analyze.rs` 的 `analyze_expr`。建议通读 `typst-ide/src/`，看 `IdeWorld`、`analyze_import`、补全与跳转如何复用同一套 `trace`/`analyze` 基础设施——这是 `trace()` 真正落地的地方。
- **对照 `eval_string` 与 `eval`**：本讲提到布局阶段的 `eval_string` 用 `Traced::default()` 不参与追踪。建议精读 `crates/typst-eval/src/lib.rs` 里 `eval` 与 `eval_string` 两个函数，体会「主模块求值（追踪）」与「上下文重求值（不追踪）」的区分。
- **思考扩展**：如果要把追踪能力从「单 span」扩展到「多 span 同时追踪」，需要改动 `Traced`（改成 `EcoVec<Span>`？）、`get` 的过滤、以及 `Vm::inspected` 的匹配逻辑。可作为一次架构推演练习。
