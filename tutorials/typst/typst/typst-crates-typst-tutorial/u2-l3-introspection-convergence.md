# 内省记录与非收敛检测

## 1. 本讲目标

上一讲（u2-l2）我们已经知道 `compile_impl` 为什么需要一个「稳定化循环」、循环怎么用 `comemo::Constraint::validate` 当作「快速通道」判定收敛、以及五轮不收敛时怎么调用 `analyze` 生成告警。但当时我们刻意把两个问题留到了本讲：

1. 循环里的 `comemo` 校验失败之后，编译器到底**凭什么**断定「文档没有收敛」？它又怎么把这个抽象结论翻译成用户能看懂的警告？
2. 为什么有时候 `comemo` 校验明明失败了，最终用户却**收不到**任何「未收敛」告警？

本讲读完，你应当能够：

- 说清 `Introspect` trait 的三个成员（`Output` / `introspect` / `diagnose`）各自承担什么职责，以及它如何抽象「一次对文档的观察」。
- 解释 `Introspection` 为什么要做「类型擦除」，以及它如何用 `Arc<dyn Bounds>` 把不同类型的内省塞进同一个 `EcoVec`。
- 跟踪 `Engine::introspect` 这一行代码：它在求值时把内省**顺手**记进 `Sink`，正是这条「记录」把稳定化循环和最终的 `analyze` 串了起来。
- 读懂 `History::converged` 用 128 位哈希判定不动点的逻辑，并解释「为什么只比较最后两个值」。
- 读懂 `analyze` 函数如何遍历历史内省器、判定收敛、生成「文档未在五次内收敛」的总告警，并解释「comemo 校验失败但文档实际收敛时不发警告」这一精妙细节。

## 2. 前置知识

本讲假定你已经读过 u2-l2，熟悉以下概念（这里只做最简回顾，不再展开）：

- **内省（introspection）**：在文档已经排版完成之后，回头去「观察」它的某些信息——比如某个标题出现在第几页、`query(heading)` 一共有几个结果、某个计数器在某处的值。`outline` 的页码、交叉引用、计数器显示都依赖内省。
- **稳定化循环**：因为内省依赖「最终布局」，而布局又会反过来受内省内容影响（比如目录页码会占行数、影响分页），所以编译器必须反复排版，直到内省结果不再变化（即到达**不动点**）。循环上限是 `MAX_ITERS = 5`。
- **`comemo::Constraint`**：u2-l2 讲过的「快速通道」。每轮排版时记录「这一轮实际问过内省器的查询」，结束后在新内省器上重放比对；全一致就判定收敛，直接 `break`，不进入本讲的 `analyze` 慢通道。
- **`Sink`**：一个「只增容器」，在求值/排版过程中收集告警、延迟错误、追踪值，以及——本讲的主角——**内省记录**。
- **`Engine`**：贯穿求值与排版的中央上下文，持有 `world`、`library`、`introspector`、`traced`、`sink`、`route` 六个字段。

如果你对以上任一概念感到陌生，请先回到 u2-l2 和 u2-l4（Engine/Sink/Route/Traced）补课。本讲聚焦「收敛是如何被**判定 + 诊断**的」，是 u2-l2 在「判定细节」和「诊断生成」两个方向上的下钻。

## 3. 本讲源码地图

本讲涉及的关键文件，以及它们各自的作用：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/introspection/convergence.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs) | **本讲的核心文件**。定义 `Introspect` trait、类型擦除的 `Introspection`、`History` 与 `converged`、以及入口 `analyze` 函数。 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | 定义 `Engine::introspect`（记录内省的入口）与 `Sink`（真正存放内省记录的容器）。 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | `compile_impl` 的稳定化循环在五轮不收敛时调用 `analyze`，把循环与诊断连成一条完整链路。 |
| [crates/typst-library/src/introspection/query.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/query.rs) | `QueryIntrospection`——一个具体的 `Introspect` 实现，本讲用作示例与实践素材。 |
| [crates/typst-library/src/introspection/counter.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs) | `CounterAtIntrospection`——另一个具体实现，展示 `Output` 是 `SourceResult<CounterState>` 的内省如何写 `diagnose`。 |
| [crates/typst-library/src/introspection/introspector.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/introspector.rs) | `Introspector` trait 与 `EmptyIntrospector`（首轮迭代用的「空内省器」）。 |

> 阅读建议：本讲按「先看抽象（`Introspect`）→ 再看它怎么被装进容器（`Introspection` / `Engine::introspect`）→ 最后看它怎么被取出来判定（`History` / `analyze`）」的顺序展开。这是一条完整的「写入 → 存放 → 读出」数据通路。

## 4. 核心概念与源码讲解

### 4.1 Introspect trait ——「一次对文档的观察」的抽象

#### 4.1.1 概念说明

「内省」是一个很宽泛的词：查标题数量是内省，取计数器在某处的值是内省，取某个标签的位置也是内省。它们形式各异，但有三点是共同的：

1. **都要等文档排完才能算**——所以都依赖一个 `Introspector`（已排版文档的「查询接口」）。
2. **都可能不收敛**——所以都需要一种方式来回答「我这次观察到的值，和历史比，稳定了吗？」
3. **不收敛时都要给用户一条看得懂的提示**——查标题数量失败了，应当说「标题数量没有稳定」；计数器失败了，应当说「计数器 X 没有稳定」。

`Introspect` trait 就是把这三点共性抽出来，定义成三个成员：

- `type Output`：这次内省产出的值的类型——**这就是「应当稳定下来」的东西**。
- `fn introspect`：给定一个内省器，算出这次的 `Output`。
- `fn diagnose`：给定这次内省的历史 `Output` 序列（即 `History`），在「没收敛」时生成一条诊断。

一句话：**一个 `Introspect` 实现就是一种「对文档的观察」，它知道怎么算、怎么判断稳定、不收敛时怎么抱怨。**

#### 4.1.2 核心流程

从「标准库某个函数想做一个内省」到「拿到结果」的过程：

```text
counter.at(loc) / query(selector) / locate(loc)
        │  构造一个具体的 Introspect 实现体（如 CounterAtIntrospection）
        ▼
Engine::introspect(introspection)        ← 4.3 节详讲
        │  1) 用当前 introspector 调 introspection.introspect() 得到 Output
        │  2) 把 introspection 包装成 Introspection 存进 Sink
        ▼
返回 Output 给标准库函数使用
```

注意第 1 步和第 2 步：**计算结果**与**记录这次内省**是同一处完成的。记录下来的东西，要等到稳定化循环五轮不收敛时，才会被 `analyze` 取出来重新审视（见 4.5）。

#### 4.1.3 源码精读

trait 定义在 [convergence.rs:93-140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L93-L140)：

```rust
pub trait Introspect: Debug + PartialEq + Hash + Send + Sync + Sized + 'static {
    type Output: Hash;

    fn introspect(
        &self,
        engine: &mut Engine,
        introspector: Tracked<dyn Introspector + '_>,
    ) -> Self::Output;

    fn diagnose(&self, history: &History<Self::Output>) -> SourceDiagnostic;
}
```

几点要点：

- 超级 trait（super-trait）里有 `Hash + PartialEq`：因为内省本身（比如「查哪个选择器」）要能被比较、去重，这会在 4.2 的类型擦除里用到。
- `type Output: Hash`（[L125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L125)）：`Output` 必须可哈希，因为收敛判定要靠哈希比较（见 4.4）。上方文档（[L94-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L94-L124)）特别提醒：`Output` 里**装多少信息**会影响收敛行为——把一个查询的结果「在外部」归约成布尔值，可能比直接把原始结果当 `Output` 早一轮收敛。
- `introspect` 拿到的是 `engine` 和 `introspector` 两个参数（[L131-L135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L131-L135)）。大多数内省只用 `introspector`（第一个参数 `engine` 写成 `_`），但计数器内省需要调用用户自定义的计数函数，所以会用到 `engine`。
- `diagnose` 只在「没收敛」时被调用（调用处用 `(!history.converged()).then(...)` 控制，见 4.2），它返回**一条** `SourceDiagnostic`，负责把这个内省失败的原因说人话。

看两个真实实现作对照。`QueryIntrospection`（[query.rs:182-203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/query.rs#L182-L203)）：

```rust
impl Introspect for QueryIntrospection {
    type Output = EcoVec<Content>;

    fn introspect(&self, _: &mut Engine, introspector: Tracked<dyn Introspector + '_>) -> Self::Output {
        introspector.query(&self.0)
    }

    fn diagnose(&self, history: &History<Self::Output>) -> SourceDiagnostic {
        let lengths = history.as_ref().map(|vec| vec.len());  // 只关心「几个结果」
        let things = format_selector(&self.0, "elements");
        let what = if !lengths.converged() {
            eco_format!("number of {things}")               // 数量在变 → 抱怨数量
        } else {
            eco_format!("query for {things}")                // 数量稳定但内容变 → 抱怨内容
        };
        format_convergence_warning(self.1, &lengths, &what)
    }
}
```

注意 `diagnose` 把 `History<EcoVec<Content>>` 通过 `as_ref().map(|vec| vec.len())` 变成了 `History<usize>`——它**只比较结果的数量**来判断「数量是否稳定」，从而生成更聚焦的提示（「结果的数量在变」还是「结果的内容在变」）。

再看计数器内省 `CounterAtIntrospection`（[counter.rs:787-814](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L787-L814)）：它的 `Output` 是 `SourceResult<CounterState>`，`introspect` 里用到了 `engine`（要跑用户的计数更新函数），`diagnose` 委托给 `format_convergence_warning`。这正是上方 trait 文档说的「计数器/状态故意不用 `QueryIntrospection`，而是写自定义内省，好让诊断更贴近用户的说法」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把项目里所有 `Introspect` 实现分类，直观感受「一次对文档的观察」可以有多少种。
2. **操作步骤**：在仓库根目录执行下面的搜索（等价于用 `Grep` 搜 `impl Introspect for`），列出所有实现体。
   ```bash
   rg "impl Introspect for" crates/typst-library/src
   ```
3. **需要观察的现象**：你会看到十多个实现，分布在 `query.rs`、`counter.rs`、`state.rs`、`location.rs`、`locator.rs`、`model/bibliography.rs`、`model/link.rs` 等文件。
4. **预期结果**：每个实现都对应一种用户能写的内省操作（`query`、`counter.at`、`state.final`、`location.page()`、`measure` 等）。挑两个实现，记下它们的 `type Output` 分别是什么类型（如 `EcoVec<Content>`、`SourceResult<CounterState>`、`NonZeroUsize`、`Option<Location>`）。这些 `Output` 类型差异很大，正是下一节要做「类型擦除」的根本原因。
5. 因为只是阅读源码，无需运行，结果可直接从源码读出。

#### 4.1.5 小练习与答案

**练习 1**：`Introspect` 的 `type Output` 为什么要约束 `: Hash`？如果允许 `Output` 不可哈希会怎样？

> **参考答案**：因为收敛判定（`History::converged`）靠「对相邻两次 `Output` 取 128 位哈希再比较」来实现（见 4.4）。若 `Output` 不可哈希，就无法用这套统一的哈希比较来判定不动点，每个内省都得各自实现一套「相等性」逻辑，trait 的抽象也就失效了。

**练习 2**：`QueryIntrospection::diagnose` 里为什么先算 `lengths = history.as_ref().map(|vec| vec.len())` 再判断？直接用原始的 `History<EcoVec<Content>>` 判断不行吗？

> **参考答案**：直接用原始 `History` 也能判断「整体是否收敛」（`converged` 比较的是 `Output` 本身的哈希），但那样就无法区分「是结果**数量**在变」还是「数量稳定、**内容**在变」。归约成数量后，`diagnose` 可以分别给出「number of … did not stabilize」或「query for … did not stabilize」两种更精准的提示，让用户更容易定位问题。

### 4.2 Introspection ——把强类型内省做类型擦除

#### 4.2.1 概念说明

上一节我们看到，每个内省的 `Output` 类型都不一样（`EcoVec<Content>`、`SourceResult<CounterState>`、`NonZeroUsize`……）。但 `Sink` 需要把一次编译里发生的**所有**内省——不管是 query、counter 还是 location——都存进**同一个**集合里，留到五轮不收敛时统一审查。

Rust 的 `Vec<T>` 要求所有元素同类型。要让「`QueryIntrospection`（带泛型 `Output`）」和「`CounterAtIntrospection`（另一个 `Output`）」住进同一个 `EcoVec`，就得**擦除它们的具体类型**，只保留一个统一的「内省记录」接口。这就是 `Introspection` 的职责：它是一个**类型擦除的包装**，内部用 `Arc<dyn Bounds>` 持有任意一种具体的内省，对外只暴露「给定历史内省器，生成一条可选诊断」这一种能力。

#### 4.2.2 核心流程

类型擦除的经典三件套在这里又一次出现：

```text
具体类型 I: Introspect
        │  Introspection::new(I) 把 I 装进 Arc<dyn Bounds>
        ▼
Introspection  (对外：只有 diagnose/dyn_eq/dyn_hash 三个动态方法)
        │  存进 Sink.introspections: EcoVec<Introspection>
        ▼
analyze 取出每个 Introspection，调 .0.diagnose(...)
        │  Bounds::diagnose 内部把 trait object 还原回具体类型 T，
        │  重新调 T 的 introspect()/diagnose()
        ▼
Option<SourceDiagnostic>
```

关键点：擦除不是「丢掉信息」，而是「把信息藏到 vtable 后面」。`Bounds::diagnose` 是一个**泛型实现**（`impl<T: Introspect> Bounds for T`），它在内部仍以具体类型 `T` 来调用 `self.introspect(...)` 和 `self.diagnose(...)`——只是这个 `T` 在外面看不到了。

#### 4.2.3 源码精读

`Introspection` 本体只有一行（[convergence.rs:144-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L144-L156)）：

```rust
#[derive(Debug, Clone, Hash)]
pub struct Introspection(Arc<dyn Bounds>);

impl Introspection {
    pub fn new<I: Introspect>(inner: I) -> Self {
        Self(Arc::new(inner))
    }
}
```

`dyn Bounds` 是真正的 trait object。`Bounds` 这个私有 trait（[L164-L172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L164-L172)）只暴露三个对象安全的方法：

```rust
trait Bounds: Debug + Send + Sync + Any + 'static {
    fn diagnose(&self, world, introspectors) -> Option<SourceDiagnostic>;
    fn dyn_eq(&self, other: &Introspection) -> bool;
    fn dyn_hash(&self, state: &mut dyn Hasher);
}
```

注意 `Bounds` 多了一个 `Any` 超级 trait——这是为了在 `dyn_eq` 里把对方 `downcast_ref` 回具体类型再比较（[L189-L195](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L189-L195)）：

```rust
fn dyn_eq(&self, other: &Introspection) -> bool {
    let inner: &dyn Bounds = &*other.0;
    let Some(other) = (inner as &dyn Any).downcast_ref::<Self>() else {
        return false;   // 类型不同 → 直接不相等
    };
    self == other       // 类型相同 → 用 Introspect 的 PartialEq
}
```

最关键的是这个 blanket impl（[L174-L203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L174-L203)），它让**任何** `T: Introspect` 自动成为 `Bounds`，并在 `diagnose` 里完成「擦除 → 还原 → 判定」的完整闭环：

```rust
impl<T: Introspect> Bounds for T {
    fn diagnose(&self, world, introspectors) -> Option<SourceDiagnostic> {
        // 用 6 个历史内省器，把这次内省在每个上面「重跑」一遍，得到 History
        let history = History::compute(world, introspectors, |engine, introspector| {
            self.introspect(engine, introspector)
        });
        // 只有「没收敛」时才生成诊断
        (!history.converged()).then(|| self.diagnose(&history))
    }
    // dyn_eq / dyn_hash 见上
}
```

这段就是「类型擦除」与「收敛判定」的交界处：外面看是 `dyn Bounds`，里面仍是强类型 `T`，调的正是 4.1 讲的 `Introspect::introspect` 与 `Introspect::diagnose`。`History::compute` 和 `converged` 在 4.4 详讲；这里只需记住 `(!history.converged()).then(...)`——**收敛了就返回 `None`（不抱怨），没收敛才返回 `Some(诊断)`**。

另外，`Introspection` 还手写了 `PartialEq`（[L158-L162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L158-L162)）和 `dyn Hash`（[L197-L209](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L197-L209)），原因是 trait object 的 `PartialEq`/`Hash` 不能自动派生，必须转调 `dyn_eq`/`dyn_hash`。`dyn_hash` 里还刻意先哈希了 `TypeId::of::<Self>()`（[L200](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L200)）——保证「类型不同、数据恰好相同」的两条内省哈希不同，避免去重误伤。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「类型擦除」为何需要 `Any` + `downcast`。
2. **操作步骤**：阅读 [convergence.rs:189-195](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L189-L195) 的 `dyn_eq`，再对比 [L200-L202](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L200-L202) 的 `dyn_hash`。
3. **需要观察的现象**：`dyn_eq` 在 `downcast_ref` 失败时直接返回 `false`；`dyn_hash` 总是先哈希 `TypeId`。
4. **预期结果**：你能解释「为什么一个 `QueryIntrospection(heading)` 和一个 `CounterAtIntrospection(page_counter)` 即使内部数据碰巧哈希相同，也绝不会被当成同一条内省」——因为 `dyn_hash` 里 `TypeId` 不同，整体哈希必然不同；`dyn_eq` 里 `downcast_ref` 也必然失败。
5. 仅阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`Introspection` 为什么用 `Arc<dyn Bounds>` 而不是 `Box<dyn Bounds>`？

> **参考答案**：因为 `Introspection` 派生了 `Clone`（见 `#[derive(Debug, Clone, Hash)]`）。求值过程中同一条内省可能被克隆（比如 `Engine::parallelize` 会把 `Sink` 拆到多个子任务再合并，见 u2-l4），`Arc` 让克隆只是增加引用计数、无需深拷贝整个内省对象。`Box` 不可 `Clone`（除非自定义深拷贝），会拖累性能。

**练习 2**：如果删掉 `dyn_hash` 里的 `TypeId::of::<Self>().hash(...)` 那一行，会出现什么错误？

> **参考答案**：两个类型不同但内部字段哈希恰好相同的内省（理论上可能）会被认为哈希相等；又因为 `Introspection` 派生了 `Hash`，若它们被放进任何依赖哈希的去重结构（如集合），就可能被错误地当成同一条内省而误删。先哈希 `TypeId` 给每种内省类型加了一个「类型盐」，从根上避免这种碰撞。

### 4.3 Engine::introspect ——求值时把内省「随手」记进 Sink

#### 4.3.1 概念说明

到目前为止我们有了 `Introspect`（抽象）和 `Introspection`（擦除后的容器），但还缺一个**入口**：标准库函数（`query`、`counter.at` 等）在做内省时，谁来负责「既算出结果、又把这次内省记下来」？

答案就是 `Engine::introspect`。它是连接「求值阶段」和「稳定化循环判定」的**唯一桥梁**：

- 它在求值/排版过程中被调用，用**当前轮的内省器**算出 `Output` 返回给标准库函数；
- 作为**副作用**，它把这条内省（包成 `Introspection`）push 进 `Sink`。

于是，稳定化循环里每一轮记录下来的 `subsink.introspections()`，就成了五轮不收敛时 `analyze` 的输入材料。没有这一步「随手记录」，`analyze` 就无米下锅。

#### 4.3.2 核心流程

把「记录」放进上一讲 u2-l2 的稳定化循环里看，整条数据通路是：

```text
compile_impl 稳定化循环（lib.rs）
  │  每轮：新建 subsink，构造 Engine（introspector = 上一轮文档的内省器）
  │
  ▼  T::create(&mut engine, ...) 触发布局/求值
求值过程中标准库函数调 engine.introspect(X)        ← 本节入口
  │  ① X.introspect(engine, 当前 introspector) → Output（返回给函数）
  │  ② sink.introspection(Introspection::new(X)) → 存进 subsink
  ▼
本轮结束：
  - 若 constraint.validate 通过（收敛）→ subsink 合并入主 sink，break
  - 若五轮不收敛 → analyze(world, 6个历史内省器, subsink.introspections())
                                   ▲
                                   └─ 这里取出的，正是上面 ② 存进去的全部内省
```

关键洞察：`analyze` 拿到的 `introspections` 列表，是**最后一轮**（第 5 轮）的 `subsink.introspections()`。但 `analyze` 不会只看最后一轮——它会对每一条内省，用 `History::compute` 在**全部 6 个历史内省器**上重新跑一遍（见 4.4），重建出这条内省在整个迭代过程中的演变史。

#### 4.3.3 源码精读

入口方法（[engine.rs:104-118](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L104-L118)）：

```rust
pub fn introspect<I>(&mut self, introspection: I) -> I::Output
where
    I: Introspect,
{
    let introspector = *self.introspector.access("is okay since we're recording it");
    let output = introspection.introspect(self, introspector);
    self.sink.introspection(Introspection::new(introspection));
    output
}
```

三行就是三件事：

1. 取出当前 `Engine` 持有的内省器句柄。注意 `self.introspector` 被包在 `Protected<...>` 里，`.access(...)` 是 comemo 提供的「绕过约束保护直接拿句柄」的方式——因为这里我们**正是要无条件记录**这次内省，不需要 comemo 的约束校验介入。
2. 调用 4.1 的 `Introspect::introspect`，用当前内省器算出 `Output`。
3. 把内省擦除成 `Introspection`（4.2），通过 `sink.introspection(...)` 存起来。

`Sink` 那一端的 `introspection` 方法是个被 comemo 追踪的 push（[engine.rs:204-209](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L204-L209)），它只是往字段 `introspections: EcoVec<Introspection>`（[L154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L154)）里 push：

```rust
pub fn introspection(&mut self, introspection: Introspection) {
    self.introspections.push(introspection);
}
```

现在看两个真实调用点，确认这条入口确实在「求值」时被触发。`query` 函数（[query.rs:173-175](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/query.rs#L173-L175)）：

```rust
context.introspect()?;
let vec = engine.introspect(QueryIntrospection(target.0, span));
Ok(vec.into_iter().map(Value::Content).collect())
```

`counter.at`（[counter.rs:370-373](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L370-L373)）：

```rust
let loc = context.location().at(span)?;
engine.introspect(CounterAtIntrospection(self.clone(), loc, span))
```

两处都是「构造一个具体内省 → 交给 `engine.introspect`」。返回的 `Output` 直接进入标准库函数的返回值，而同一条内省也同时被记进了 `Sink`。

最后回到 `compile_impl`，看「记录」如何变成 `analyze` 的输入（[lib.rs:163-175](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L163-L175)）：

```rust
if history.is_full() {
    let mut introspectors = [&empty_introspector as &dyn Introspector; MAX_ITERS + 1];
    for i in 1..MAX_ITERS {
        introspectors[i] = history[i - 1].introspector();
    }
    introspectors[MAX_ITERS] = document.introspector();

    let warnings = typst_library::introspection::analyze(
        world,
        introspectors,
        subsink.introspections(),   // ← 第 5 轮记录下的全部内省
    );
    ...
}
```

`subsink.introspections()`（[engine.rs:179-181](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L179-L181)）取出的，正是本节 `Engine::introspect` 在第 5 轮里一条条 push 进去的内省。`introspectors` 数组则是 6 个历史内省器（下节详讲其构成）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：跟踪一条完整的「调用 → 记录」链路，确认 `Engine::introspect` 是唯一入口。
2. **操作步骤**：
   - 在 [query.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/query.rs) 中找到 `engine.introspect(QueryIntrospection(...))`（L174）。
   - 跳到 [engine.rs:109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L109) 的 `Engine::introspect`。
   - 再跳到 [engine.rs:207](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L207) 的 `Sink::introspection`。
   - 最后跳到 [lib.rs:174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L174) 的 `subsink.introspections()`。
3. **需要观察的现象**：从「用户写 `query(heading)`」到「内省出现在 `analyze` 的入参里」，全程只经过这一条链路，没有任何其他地方手动 push 内省。
4. **预期结果**：你能用一句话概括——「`Engine::introspect` 是内省唯一的写入点，`Sink.introspections` 是唯一的存储点，`analyze` 是唯一的读出点」。
5. 仅阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`Engine::introspect` 里为什么用 `self.introspector.access(...)` 而不是直接 `*self.introspector`？

> **参考答案**：`self.introspector` 的类型是 `Protected<Tracked<dyn Introspector>>`，`Protected` 是 comemo 用来防止「在约束校验之外偷偷访问被追踪对象」的封装。这里我们要无条件记录内省（不希望它被 comemo 的约束机制拦截或失效），所以用 `.access(...)` 显式声明「我知道自己在做什么，给我句柄」。注释 `"is okay since we're recording it"` 正是在向 comemo 解释这一意图。

**练习 2**：如果某条内省在求值过程中**根本没被触发**（比如 `query` 在这一轮没被调用），它还会出现在 `analyze` 的 `introspections` 里吗？

> **参考答案**：不会。`Sink.introspections` 是「按需记录」的——只有真正调用了 `engine.introspect` 的内省才会被 push 进去。某轮没触发的内省，那一轮的 `subsink` 里就没有它。不过这不影响 `analyze` 的正确性：`History::compute` 会对列表里**存在**的每条内省，在全部 6 个历史内省器上重跑，重建其演变史（见 4.4）。

### 4.4 History::converged ——用哈希判定不动点

#### 4.4.1 概念说明

`analyze` 拿到一条内省后，要回答的核心问题是：**「这条内省，在整个 5 轮迭代里，到底稳没稳定？」** 为此它需要这条内省在每一轮的取值——这就是 `History`。

`History` 是一个长度固定的数组，记录「每个历史内省器 + 这条内省在该内省器上的取值」。有了它，`converged` 就能用一个简单的哈希比较来判定不动点。

这里要先厘清 `History` 的**长度与下标含义**，这是本讲最容易绕晕的地方。先看几个常量（[convergence.rs:16-20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L16-L20)）：

```rust
pub const MAX_ITERS: usize = 5;
pub const ITER_NAMES: &[&str] = &["iter (1)", "iter (2)", "iter (3)", "iter (4)", "iter (5)"];
const INSTANCES: usize = MAX_ITERS + 1;   // = 6
```

`MAX_ITERS = 5` 是排版迭代上限；`INSTANCES = 6` 是历史内省器的数量。回到 u2-l2 的循环：`history` 是容量 4 的 `ArrayVec`，存第 1~4 次排版的文档；第 5 次排版得到 `document` 但不再 push。当 `history.is_full()`（4 个文档）时，`compile_impl` 拼出 6 个内省器传入 `analyze`（[lib.rs:164-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L164-L169)）：

| 下标 `i` | 来源 | 含义 |
| --- | --- | --- |
| 0 | `empty_introspector` | 第 1 轮排版时观察的内省器（空） |
| 1 | `history[0]`（第 1 个文档） | 第 2 轮排版时观察的内省器 |
| 2 | `history[1]`（第 2 个文档） | 第 3 轮排版时观察的内省器 |
| 3 | `history[2]`（第 3 个文档） | 第 4 轮排版时观察的内省器 |
| 4 | `history[3]`（第 4 个文档） | 第 5 轮排版时观察的内省器 |
| 5 | `document`（第 5 个文档，当前产物） | 第 5 轮排版的**结果** |

> 一句话：下标 `i`（`0..=4`）是「第 `i+1` 轮排版**所用**的内省器」，下标 `5` 是「第 5 轮排版**产出**的内省器」。

`History::compute` 会对这 6 个内省器**逐一重跑**这条内省，得到 6 个 `Output` 值，于是 `History.0` 的下标与上表完全对应。

#### 4.4.2 核心流程

`History::compute`（[convergence.rs:217-236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L217-L236)）为每个历史内省器**临时搭一个 `Engine`**，重跑内省：

```rust
fn compute(world, introspectors, f) -> Self {
    Self(introspectors.map(|introspector| {
        let tracked = introspector.track();
        let mut engine = Engine {
            library: world.library(),
            world,
            introspector: Protected::new(tracked),
            traced: Traced::default().track(),
            sink: sink.track_mut(),
            route: Route::default(),
        };
        (introspector, f(&mut engine, tracked))   // f = T::introspect
    }))
}
```

注意它给每个内省器都建了**全新**的 `sink`/`traced`/`route`——这是一次「干净的离线重放」，不污染主编译的 `Sink`，也不受当前调用栈深度影响。`f` 就是 4.1 的 `Introspect::introspect`，所以这里等于「把这条内省在 6 个历史文档上各算一遍」。

得到 `History` 后，判定收敛（[convergence.rs:240-250](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L240-L250)）：

```rust
pub fn converged(&self) -> bool where T: Hash {
    typst_utils::hash128(&self.0[MAX_ITERS - 1].1)
        == typst_utils::hash128(&self.0[MAX_ITERS].1)
}
```

也就是比较 `History.0[4]` 和 `History.0[5]` 的 128 位哈希——**只比最后两个**。

用数学语言写清楚。设这条内省在 6 个历史内省器上的取值为 \( v_0, v_1, \dots, v_5 \)（下标对应上表），\( H \) 为 128 位哈希函数，则收敛判定为：

\[
\mathrm{converged} \iff H(v_4) = H(v_5)
\]

为什么只比 \( v_4 \) 和 \( v_5 \) 就够了？因为 \( v_4 \) 是「观察第 4 个文档」得到的值，而第 5 个文档正是**基于 \( v_4 \) 这个观察**排出来的；\( v_5 \) 则是「观察第 5 个文档」得到的值。若 \( v_4 = v_5 \)，说明「用第 4 个文档算出的内省值」与「用据此排出的第 5 个文档算出的内省值」一致——也就是说，把第 5 个文档再喂回去排第 6 个，内省值不会变，文档也不会变。这正是**不动点**的定义。

代码注释（[L244-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L244-L249)）还点出一个用哈希而非 `==` 的理由：状态从浮点 `0.0` 变成整数 `0`，语义上「下一轮可观察到差异」，不应算收敛——而哈希能区分这两个不同类型的值（它们的 `Hash` 实现不同），纯 `PartialEq` 不一定能捕捉到。

#### 4.4.3 源码精读

`History` 结构体（[convergence.rs:211-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L211-L213)）：

```rust
pub struct History<'a, T>([(&'a dyn Introspector, T); INSTANCES]);
```

它同时保存了「内省器引用」和「重算的值」——内省器引用是给 `diagnose` 里需要进一步查询时用的。除了 `compute`/`converged`，还有几个辅助方法：

- `map`（[L253-L255](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L253-L255)）：变换值类型，保留内省器。`QueryIntrospection::diagnose` 就是用它把 `History<EcoVec<Content>>` 变成 `History<usize>`（取长度）。
- `as_ref`（[L258-L260](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L258-L260)）：把 `History<T>` 变成 `History<&T>`，避免克隆。
- `final_introspector`（[L263-L265](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L263-L265)）：取下标 `MAX_ITERS`（即第 5 个文档）的内省器。
- `hint`（[L268-L280](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L268-L280)）：把 6 个值格式化成一条提示。下标 `0..MAX_ITERS`（0~4）标为 `run 1`~`run 5`，下标 `MAX_ITERS`（5）标为 `final`——与上表的语义完全对应。

`hint` 的输出长这样（伪示例）：

```text
the following numbers of elements were observed:
- run 1: 0
- run 2: 2
- run 3: 2
- run 4: 3
- run 5: 3
- final: 3
```

它会作为诊断的 hint 附在告警里（见 `QueryIntrospection::diagnose` 与 `format_convergence_warning`），让用户直接看到这条内省在每一轮的取值演变。

#### 4.4.4 代码实践（计算型）

1. **实践目标**：用具体数值走通 `converged` 的判定，体会「只比最后两个」。
2. **操作步骤**：假设一条 `QueryIntrospection(heading)` 在 6 个历史内省器上重跑得到结果数量序列 `History.0[*].1 = [0, 2, 2, 3, 3, 3]`（依次对应 run1~run5、final）。代入 `converged` 的公式手算。
3. **需要观察的现象**：`converged` 取 `self.0[4].1`（值 3）和 `self.0[5].1`（值 3），比较 `hash128(3) == hash128(3)`。
4. **预期结果**：相等 → `converged() == true` → `Bounds::diagnose` 里 `(!true).then(...)` 得 `None` → 这条内省**不产生**诊断。注意它前面 run1→run2→run3 有过波动（0→2→2→3），但因为 `converged` 只看最后两位，仍然判定为收敛。
5. **思考延伸**：把序列改成 `[0, 1, 0, 1, 0, 1]`（振荡），则 `self.0[4]=0`、`self.0[5]=1` → 哈希不等 → 不收敛 → 产生诊断。本实践纯纸笔计算，无需运行；如想在真实编译里复现振荡，可用 `#let n = query(heading).len(); #if calc(even(n)) [= 偶]` 之类自指结构，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`converged` 比的是 `self.0[MAX_ITERS - 1]`（下标 4）和 `self.0[MAX_ITERS]`（下标 5）。为什么不是比较「相邻两轮」比如下标 3 和 4？

> **参考答案**：因为下标 5（`final`，第 5 个文档的内省器）才是「最新、最完整」的产物。下标 4 是「第 5 轮排版时所观察的」（第 4 个文档），下标 5 是「第 5 轮排版所产出的」（第 5 个文档）。比较 4 和 5，就是在问「用上一轮结果算出的内省值，与用这一轮产物算出的内省值，是否一致」——这正是不动点判据。比较下标 3 和 4 只能说明「第 4 轮相对第 3 轮是否稳定」，而第 4 轮并非最终产物，判错不动点。

**练习 2**：`History::compute` 给每个内省器都建了全新的 `Sink` 和 `Route::default()`。如果不新建、直接复用主编译的 `Engine`，会有什么问题？

> **参考答案**：两个问题。其一，重放会污染主 `Sink`——重算时标准库函数可能再次触发 `engine.introspect`，导致同一条内省被重复记录，或往主 sink 塞入过时告警。其二，`Route` 反映当前调用栈深度（用于检测循环导入/过深嵌套，见 u2-l4），重放时应从根开始（`Route::default()`），复用主 `Route` 会让深度判断失真。所以离线重放必须用干净的上下文。

### 4.5 analyze ——未收敛时生成可读诊断

#### 4.5.1 概念说明

`analyze` 是本讲的总入口，也是 `compile_impl` 稳定化循环在「五轮不收敛」时调用的函数（u2-l2 已点过）。它要做三件事：

1. 取出第 5 轮 `subsink` 里记录的全部内省（4.3 存进去的）；
2. 对每一条，用 6 个历史内省器重建 `History`（4.4），判定是否收敛；没收敛的才调它的 `diagnose` 生成一条诊断（4.1/4.2）；
3. 把所有诊断汇总，若有任何一条，就在最前面追加一条「总告警」，告诉用户「文档没在五次内收敛」。

但本讲最微妙、也最值得品味的细节，是 `analyze` 里那段长注释揭示的情形：**`comemo` 校验失败（所以才会进 `analyze`），但文档其实已经收敛——此时 `analyze` 不发任何警告。** 这是怎么做到的？关键就在「`comemo` 的收敛观」与「`Introspect` 的收敛观」**不一定相同**。

#### 4.5.2 核心流程

```text
compile_impl：constraint.validate 失败（comemo 认为没收敛）
        │
        ▼ 进入 analyze 慢通道
analyze(world, [6 个内省器], subsink.introspections())
        │
        ▼  对每一条 Introspection：
Bounds::diagnose
   ├─ History::compute：在 6 个内省器上重跑，得 History
   ├─ history.converged()? ── 是 → 返回 None（不抱怨）
   │                        ── 否 → 调 Introspect::diagnose 生成一条诊断
        │
        ▼ 汇总所有诊断 diags
if diags 非空:
    在最前面插入「document did not converge within five attempts」总告警
    （并附 hint：见 N 条附加警告、见帮助链接）
返回 diags（可能为空 → compile_impl 不加任何收敛警告）
```

#### 4.5.3 源码精读

`analyze` 主体（[convergence.rs:24-70](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L24-L70)）：

```rust
pub fn analyze(
    world: Tracked<dyn World + '_>,
    introspectors: [&dyn Introspector; INSTANCES],
    introspections: &[Introspection],
) -> EcoVec<SourceDiagnostic> {
    let mut sink = Sink::new();
    for introspection in introspections {
        if let Some(warning) = introspection.0.diagnose(world, introspectors) {
            sink.warn(warning);
        }
    }
    // ...（下方生成总告警）
}
```

`introspection.0.diagnose(...)` 通过 `dyn Bounds` 分派到 4.2 的 blanket impl，内部完成 `History::compute` + `converged` 判定。注意这里用了 `Sink::warn` 而非直接 push——`warn` 会按 `(span, message)` 哈希去重（[engine.rs:222-228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L222-L228)），所以即便同一条内省被记录多次，告警也不会重复。

接下来是本讲最关键的一段注释 + 代码（[convergence.rs:37-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L37-L69)）。注释用「两种写法」的对比点破了 `comemo` 与 `Introspect` 收敛观的差异：

- **写法 (1)**：用 `QueryIntrospection` 拿到全部匹配元素，再**在外部**判断是否为空。这时内省观察到的数据和 `comemo` 观察到的**完全相同**——内省收敛 ⟺ `comemo` 校验通过。所以一旦进了 `analyze`（说明 `comemo` 失败），这条内省必然也不收敛，`diagnose` 会返回 `Some`，**会**产生警告。
- **写法 (2)**：写一个**自定义**内省，内部做完查询后**归约成一个布尔**（比如「是否至少有一个元素」）。这时内省**过滤掉了** `comemo` 实际观察到的部分数据。于是可能出现：`comemo` 校验失败（因为它看到的数据还在变），但布尔值其实早就稳定了——文档**实际收敛**。此时 `History::converged` 返回 `true`，`diagnose` 返回 `None`，`analyze` 收集到 0 条诊断。

注释明确道出结论（[L48-L51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L48-L51)）：在写法 (2) 下，「我们到达了 `analyze`（因为 `comemo` 校验失败），但拿到零条诊断。这时我们**不**发出收敛警告，因为文档其实收敛了」。这就是学习目标里「comemo 校验失败但文档实际收敛时不发警告」的来源。

落实到代码，就是最后这段（[L56-L69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L56-L69)）：

```rust
let mut diags = sink.warnings();
if !diags.is_empty() {
    let summary = warning!(
        Span::detached(),
        "document did not converge within five attempts";
        hint: "see {} additional warning{} for more details", diags.len(), if diags.len() > 1 { "s" } else { "" };
        hint: "see https://typst.app/help/convergence for help";
    );
    diags.insert(0, summary);
}
diags
```

只有 `diags` **非空**时，才插入那条总告警。若 `diags` 为空（写法 (2) 的情形），`analyze` 直接返回空 `EcoVec`。回到 `compile_impl`（[lib.rs:177-181](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L177-L181)）：

```rust
sink.extend_from_sink(subsink);
for warning in warnings {     // warnings 为空 → 循环不执行
    sink.warn(warning);
}
break;
```

于是没有任何收敛告警进入主 `sink`，用户毫不知情——这正是期望的行为（文档既然真的收敛了，就不该用假警告打扰用户）。注释最后还提到（[L53-L55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L53-L55)）：本来完全可以不依赖 `comemo`、纯靠 `Introspect` 来判收敛，但把 `comemo` 当「快速通道」很划算——绝大多数文档第一轮就通过 `constraint.validate` 收敛，根本不必走到 `analyze` 这个昂贵的慢通道。

#### 4.5.4 代码实践（分析型，对应大纲指定任务）

1. **实践目标**：选一种已有内省，对照 `convergence.rs` 写出它在 5 次迭代中 `History` 的取值变化，并判断最终是否会产生非收敛告警。
2. **操作步骤**：选定 `QueryIntrospection`（`type Output = EcoVec<Content>`，`diagnose` 关心的是 `vec.len()`）。设想一个**自指、振荡**的文档，使得每轮排版时匹配到的标题数量在两个值之间来回跳：构造 `History.0[*].1`（长度序列）如下表。

   | 下标 | 标签 | 假设的匹配数量 |
   | --- | --- | --- |
   | 0 | run 1 | 0 |
   | 1 | run 2 | 1 |
   | 2 | run 3 | 0 |
   | 3 | run 4 | 1 |
   | 4 | run 5 | 0 |
   | 5 | final | 1 |

3. **需要观察的现象**：套用 `converged`：比较 `History.0[4].1 = 0` 与 `History.0[5].1 = 1`。
4. **预期结果**：`hash128(0) != hash128(1)` → `converged() == false` → `Bounds::diagnose` 调用 `QueryIntrospection::diagnose`，生成「number of … elements did not stabilize」警告，`History::hint` 把上表的 6 个数量贴成 hint。`analyze` 检测到 `diags` 非空，在最前面追加总告警「document did not converge within five attempts」，并提示「见 1 additional warning」。最终用户看到 **2 条**诊断（1 条总告警 + 1 条具体内省告警）。
5. **对照情形**：若把上表最后两格都改成 `3`（即 `[0,1,0,1,3,3]`），则 `converged()` 比较 `3` 与 `3` → 相等 → 不产生诊断。但要注意：对 `QueryIntrospection`（写法 1）而言，这种「前面振荡、最后两位恰好相等」的情形，`comemo` 通常也会判收敛而不会进 `analyze`；真正能体现「comemo 失败却零诊断」的是写法 (2) 的自定义归约内省。振荡文档的真实复现**待本地验证**（可尝试用 `counter.update` 与显示值耦合的自指结构触发）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `analyze` 用 `sink.warn(warning)` 收集诊断，而不是 `sink.delayed_error` 或直接 push？

> **参考答案**：因为「未收敛」对用户是**警告（warning）**而非致命错误——文档仍然成功产出了（只是内省可能不准）。用 `warn` 一方面语义正确（不阻断编译），另一方面 `Sink::warn` 内置按 `(span, message)` 哈希去重（[engine.rs:224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L224)），能避免同一条内省被重复记录时产生重复告警。

**练习 2**：请用自己的话解释「comemo 校验失败但文档实际收敛时不发警告」这一现象，并指出是 `convergence.rs` 的哪一行代码保证了这一点。

> **参考答案**：`comemo` 的 `constraint.validate` 关注的是「求值时实际发起的查询在新文档上是否仍一致」；而某个自定义内省可能把查询结果**归约**（如压成布尔），只关心归约后的值是否稳定。于是可能出现「原始查询还在变（comemo 失败）、但归约值已稳定（文档实际收敛）」。保证「此时不发警告」的关键链是：`History::converged` 返回 `true`（[convergence.rs:248-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L248-L249)）→ `Bounds::diagnose` 里 `(!true).then(...)` 返回 `None`（[L186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L186)）→ `analyze` 收集到空 `diags` → `if !diags.is_empty()` 不成立，不插总告警（[L57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L57)）→ 返回空 → `compile_impl` 不加任何收敛警告。

## 5. 综合实践

把本讲五个模块串起来，完成下面这个「全链路追踪」任务。设想用户写了这样一个会产生振荡的 Typst 文档（**此为说明性示例，振荡行为的真实复现待本地验证**）：

```typ
// 一个内省结果反过来影响自身是否出现的自指结构
#let n = counter("my").at(here())
#context if calc(even(n.first())) [
  = 标题 A
]
#counter("my").update(1)
```

请按下列步骤，对照源码写出一份「追踪报告」：

1. **触发入口**：指出 `counter.at` 在哪里调 `engine.introspect`（[counter.rs:372](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L372)），说明这一步同时做了「算 `CounterState`」和「记录 `CounterAtIntrospection`」两件事（4.3）。
2. **被记录**：说明这条 `CounterAtIntrospection` 经 `Introspection::new` 类型擦除后（4.2），被 push 进每一轮的 `subsink.introspections`。
3. **慢通道触发**：说明 `compile_impl` 在第 5 轮 `constraint.validate` 失败后，拼出 6 个历史内省器并调 `analyze`（[lib.rs:171-175](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L171-L175)）。
4. **重建历史**：在 `analyze` → `Bounds::diagnose` → `History::compute`（[convergence.rs:217-236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L217-L236)）里，这条计数器内省在 6 个历史内省器上各重跑一次。请**推测并写出** 6 次 `CounterState` 取值（提示：标题 A 是否出现取决于计数器奇偶，可能振荡）。
5. **判定**：代入 `converged`（[convergence.rs:248-249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L248-L249)）比较下标 4 与 5，判断是否收敛。
6. **诊断**：若不收敛，`CounterAtIntrospection::diagnose`（[counter.rs:811-813](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/counter.rs#L811-L813)）会经 `format_convergence_warning` 生成一条「计数器没稳定」的告警，`History::hint` 把 6 个 `CounterState` 贴成 hint；`analyze` 再追加一条总告警。
7. **复盘**：用一段话说明，为什么这条计数器内省属于 4.5 注释里的「写法 (1)」（观察的数据与 comemo 一致），所以一旦进了 `analyze` 就必然产生告警；并对比「写法 (2)」自定义归约内省在同样情形下可能零告警的差异。

> 提示：如果你想在本地真的看到这条告警，可以逐步简化上面的文档直到出现 `document did not converge within five attempts`，对照 `https://typst.app/help/convergence` 阅读官方对收敛问题的排查建议。

## 6. 本讲小结

- `Introspect` trait 用 `Output` / `introspect` / `diagnose` 三件套，把「一次对文档的观察」抽象到统一接口：`Output` 是「应当稳定的东西」，`introspect` 算它，`diagnose` 在不收敛时把它说人话。
- `Introspection` 是类型擦除的包装（`Arc<dyn Bounds>`），让 `Output` 各异的各种内省能住进同一个 `EcoVec`；`Any + downcast` 与「先哈希 `TypeId`」保证了不同类型内省不会被误判相等。
- `Engine::introspect` 是内省的**唯一写入点**：它用当前内省器算出 `Output` 返回给标准库函数，同时把这条内省顺手记进 `Sink`——这一步把求值阶段与稳定化循环的判定连了起来。
- `History` 在 6 个历史内省器（`INSTANCES = MAX_ITERS + 1`）上离线重放某条内省；`converged` 只比较最后两个值（下标 `MAX_ITERS-1` 与 `MAX_ITERS`）的 128 位哈希，因为「上一轮观察值」与「这一轮产物上的观察值」一致即为不动点。
- `analyze` 是慢通道入口：`comemo` 校验失败后才进入；对每条内省重建 `History` 判定，没收敛才生成诊断；只要有任何诊断，就追加一条「document did not converge within five attempts」总告警。
- 精妙之处在于：`comemo` 与 `Introspect` 的「收敛观」不一定相同。自定义归约内省可能出现「comemo 失败、但归约值已稳定」的情形，此时 `History::converged` 返回 `true` → 零诊断 → 不发任何警告，避免对已收敛文档的假阳性打扰。

## 7. 下一步学习建议

本讲把「收敛的判定与诊断」讲透了，接下来建议：

- **横向补全上下文对象**：本讲反复提到的 `Sink`（去重、延迟错误、追踪值）、`Route`（循环导入与深度检测）、`Traced`（按文件 id 过滤 span）都定义在 `engine.rs`。如果你还没系统读过，强烈建议进入 **u2-l4（Engine / Sink / Route / Traced）**，把 `Sink` 的四个职责（内省 / 延迟错误 / 告警去重 / 追踪值）一次性看清。
- **纵向看具体内省**：挑一个 4.1 练习里列出的 `Introspect` 实现（推荐 `counter.rs` 的三个、`state.rs` 的两个、`location.rs` 的 `PageIntrospection`/`PositionIntrospection`），对照本讲的框架，自己讲一遍它的 `Output` 是什么、`diagnose` 怎么写、在 `History` 里会怎么演变。
- **进入专家层**：收敛机制是 `compile_impl` 内部最深的一块。掌握之后，可以转向 **u3 单元**的架构视角——`Target`/`Output` 多目标抽象（u3-l1）、`ROUTINES` 函数指针表与 crate 切分（u3-l2）、诊断去重与友好提示（u3-l4，与本讲的 `Sink::warn` 去重、`hint_invalid_main_file` 呼应）、以及与 `compile` 并列的 `trace()` 值追踪（u3-l5，它复用的正是本讲 `Sink.values` 那一侧）。
- **阅读官方帮助**：`analyze` 总告警里指向的 `https://typst.app/help/convergence` 是用户视角的收敛问题排查指南，结合本讲的实现视角阅读，能同时获得「用户怎么修」和「编译器怎么发现」两个层面的理解。
