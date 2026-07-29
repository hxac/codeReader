# 并行与记忆化：rayon + comemo 的性能设计

> 本讲是专家层的第 3 篇。在前几讲里，你已经分别见过了 bundle 的编译主流程（u2）、导出层（u4）和统一内省器（u5-l1）、跨文档链接（u5-l2）。本讲把视角拉高：**不新增功能**，而是追问 bundle 流水线里两个贯穿始终的工程手段——**并行（rayon）** 与 **记忆化（comemo）**——是如何分工、如何协作，并共同支撑起 bundle 最特殊的需求：**让多个文档在一次编译里互相内省、并迭代到收敛**。

## 1. 本讲目标

学完本讲，你应当能够：

- 在源码里**准确定位** bundle 的三个 rayon 并行点（编译、导出、锚点生成）和五个 comemo 记忆化点（`bundle_impl` 与四个 `export_*`）。
- 说清每个并行点「为什么可以安全并行」、每个记忆化点「缓存了什么、键是什么」。
- 解释 `Engine::parallelize` 是如何用「每个子任务一个独立 `Sink`、事后合并」来安全收集并行错误的。
- **核心**：讲明白为什么 `bundle_impl` 的执行顺序是「先并行编译各文档 → 再统一构造 `BundleIntrospector::new` → 再 `set_anchors`」，以及这套顺序如何保证「跨文档互相内省」的正确性与可收敛性。
- 理解 comemo 的 `Constraint` 机制如何兼作内省收敛检测器，使「并行 + 记忆化」不是巧合叠加，而是协同设计。

## 2. 前置知识

本讲默认你已经读过 u2-l1（编译主流程）、u4-l1（导出主流程）、u5-l1（统一内省器）。下面三个背景概念是关键：

- **内省（introspection）与收敛**：Typst 文档里，页码、计数器、查询结果等依赖「排版结果本身」。因此编译是一个**不动点迭代**：拿上一轮的内省结果当输入，重新排版，得到新的内省结果；若两轮一致则收敛。用公式表达即

  \[
  I_{n+1} = F(I_n), \qquad \text{收敛当且仅当}\ I_{N} = I_{N-1}.
  \]

  bundle 的特殊之处在于：**多个文档共用同一个内省器**，互相可见，构成一个大的不动点。

- **rayon**：Rust 的数据并行库。`par_iter()` / `par_iter_mut()` / `into_par_iter()` 把一个集合的工作切分到一个线程池上并行执行。bundle 里凡是「逐文档独立」的工作都适合并行。

- **comemo**：Typst 自研的增量/记忆化框架。它的两个能力本讲都会用到：
  - `#[comemo::memoize]`：按输入身份缓存函数结果，输入不变则直接返回缓存。
  - `Tracked<T>` + `Constraint`：给一个值发一张「被追踪」的廉价句柄，comemo 记录计算过程中**读了它的哪些部分**；事后可以用 `constraint.validate(新值)` 反问「换成这个新值，结果会不会变」。

> 小提示：`Tracked<T>` 参与缓存键的是**身份（identity）**而非完整内容哈希，所以跨记忆化边界传递一个 tracked 句柄很廉价。这一点会反复出现。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/typst-bundle/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs) | 数据模型 + 编译入口 | `bundle_impl` 的记忆化、`parallelize` 并行编译、`BundleIntrospector::new` + `set_anchors` 的收尾顺序 |
| [crates/typst-bundle/src/export.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs) | 把 `Bundle` 序列化为 `VirtualFs` | `export()` 的 `par_iter` 并行导出、四个 `export_*` 的记忆化 |
| [crates/typst-bundle/src/link.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs) | 跨文档链接锚点 | `create_link_anchors` 的 `par_iter_mut` 并行 |
| [crates/typst-bundle/src/introspect.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs) | 统一内省器 | `BundleIntrospector::new` / `set_anchors`：并行的「汇合点」 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs) | 引擎与 `parallelize` | `Engine::parallelize` 的「子 Sink 合并」机制 |
| [crates/typst-library/src/diag.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs) | 诊断聚合 | `ParallelCollectCombinedResult`：并行收集「全部错误」而非短路 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs) | 顶层 `compile_impl` | 内省迭代循环与 `constraint.validate` 收敛判定 |

## 4. 核心概念与源码讲解

### 4.1 rayon 并行：编译、导出、锚点生成的三处并行点

#### 4.1.1 概念说明

bundle 一次编译会产出**多个文件**。绝大多数工作都是「逐文件独立」的：文档 A 的排版不直接修改文档 B 的字节，文档 B 的导出也不依赖文档 A 的导出结果。这种「彼此无数据依赖」的工作负载叫做 **embarrassingly parallel（令人尴尬的并行）**，非常适合 rayon。

bundle 一共只有**三个** rayon 并行点，分别覆盖流水线的三个阶段：

1. **编译阶段**：并行地把每个 `#document` 排版成 `BundleDocument`。
2. **导出阶段**：并行地把每个 `BundleFile` 编码成字节。
3. **锚点生成阶段**：并行地为每个文档生成跨文档链接锚点。

为什么没有更多并行点？因为 `realize` / `collect`（结构校验）、`BundleIntrospector::new`（合并所有文档）本质上是**全局串行**的——它们必须看到全部文档后才能做判断，没有可拆分的独立性。

#### 4.1.2 核心流程

三处并行点的共同形态是「`par_*` 遍历 + 各自独立处理 + 最后汇总」。其中编译阶段最复杂，因为它要把并行子任务里产生的**错误/延迟错误/警告**安全地收集回主引擎——这靠 `Engine::parallelize` 的「子 Sink」机制实现，流程如下：

```text
主引擎 engine
  │  parallelize(children, 闭包 f)
  ▼
┌──────────────────────────────────────────────┐
│ work = children 收集成 Vec（保序）             │
│ work.into_par_iter().map(|child| {            │
│     sink = Sink::new()           ← 每个任务独有 │
│     engine' = {共享只读字段, sink: sink}       │
│     (f(engine', child), sink)                 │
│ }).collect_into_vec(pairs)                    │
└──────────────────────────────────────────────┘
  │  全部任务跑完后，串行合并
  ▼
for (_, sink) in &mut pairs:
    主 engine.sink.extend(sink.introspections/delayed/warnings/...)
```

关键点：**并行期间每个子任务持有一个独立的 `Sink`**，互不干扰；等所有任务结束，主线程再把各个子 `Sink` 的内容串行 `extend` 回主 `Sink`。这就是为什么 u2-l1 讲过的 `delayed_error`（路径重复、PNG/SVG 单页约束）即便在并行编译中发出，最终也能被正确收集——它先进子 `Sink`，再被合并。

#### 4.1.3 源码精读

**(1) 编译阶段的并行：`bundle_impl` 中的 `engine.parallelize`**

`bundle_impl` 用 `engine.parallelize` 把 `collect` 得到的 `children` 并行编译，每个 `Child::Document` 走 `compile_document`：

[crates/typst-bundle/src/lib.rs:179-195](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179-L195) — 对每个 child 并行分派：`Tag` 透传、`Asset` 抽出字节与 Location、`Document` 调用 `compile_document` 排版；最后 `collect_combined_result::<Vec<_>>()` 汇总。

注意这行之后的 `.collect_combined_result::<Vec<_>>()?`：它不是短路式 `?`，而是**先让所有任务跑完、再合并全部错误**（见 4.1.2 与 diag.rs 的实现）。

**(2) `Engine::parallelize` 的子 Sink 合并**

[crates/typst-library/src/engine.rs:53-102](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L53-L102) — 先 `collect` 成 `Vec`（注释说明不用 `par_bridge` 是因为它**不保序**），再 `into_par_iter`，每个任务 `Sink::new()` 起一个独立 sink；[engine.rs:90-99](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L90-L99) 把各子 sink 的 `introspections / delayed / warnings / values` 全部 `extend` 回主 sink。

> 子任务之间共享的是**只读**字段（`world` / `library` / `introspector` / `traced` / `route`），可写的只有各自的 `sink`。这是「可并行」的根本前提。

**(3) 导出阶段的并行：`export` 中的 `par_iter`**

[crates/typst-bundle/src/export.rs:24-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L24-L40) — `bundle.files.par_iter()`：`Document` 分支构造 `LateLinkResolver::new(Some(path), ...)` 后调 `export_document`，`Asset` 分支直接 `bytes.clone()` 直通；`.collect_combined_result()` 合并。

这里用的是 `ParallelCollectCombinedResult`，它的实现就是「先 `collect::<Vec<_>>()`，再走串行版的 `collect_combined_result`」：

[crates/typst-library/src/diag.rs:263-279](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L263-L279) — 注释直言「更高效的写法可能存在，但这样更简单务实；本 trait 主要是为让调用点方便，不是为了榨干性能」。这也印证了 bundle 在并行上「 correctness 优先、性能其次」的态度。

**(4) 锚点生成阶段的并行：`create_link_anchors` 中的 `par_iter_mut`**

[crates/typst-bundle/src/link.rs:25-49](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L25-L49) — `items.par_iter_mut()`：对每个 `Item::Document` 并行生成锚点。HTML 走 `typst_html::create_link_anchors` **原地改写 DOM** 插入 `id`；paged 走 `create_paged_link_anchors` 把命名目的地写入 `PagedExtras.anchors`。因为每个文档改的是**自己**的 DOM / 自己的 `PagedExtras`，互不触碰，所以 `&mut` 并行是安全的。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里标出三处并行点，并验证「错误可被并行收集」。

**操作步骤**：

1. 打开 [lib.rs:179](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179)、[export.rs:27](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L27)、[link.rs:26](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L26)，分别标注「编译并行 / 导出并行 / 锚点并行」。
2. 阅读并行收集错误的对比：在 [diag.rs:231-252](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L231-L252) 中找到串行版 `collect_combined_result` 的实现，确认它是「`filter_map` 把 `Ok` 取出、把 `Err` 累加进 `errors`」，即**不短路**。

**需要观察的现象**：

- 三处并行点都出现在「逐文件」的循环上，且后面都紧跟一个 `collect_combined_result` / 等价的汇总步骤。
- `parallelize` 内部对每个任务 `Sink::new()`（[engine.rs:77](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L77)），事后 `extend`（[engine.rs:91-99](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L91-L99)）。

**预期结果**：你能用一句话说出「bundle 把并行限定在三个逐文件阶段，并用『子 Sink + 事后合并』和『不短路汇总』保证并行中的错误不丢、顺序不乱」。

#### 4.1.5 小练习与答案

**练习 1**：`Engine::parallelize` 为什么先 `iter.into_iter().collect()` 成 `Vec` 再 `into_par_iter`，而不是直接用 `par_bridge`？

> **参考答案**：源码注释（[engine.rs:69-71](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L69-L71)）说明 `par_bridge` **不保序**；而 bundle 需要保留文档的输入顺序（`files` 是 `IndexMap`，插入顺序即输出顺序），所以必须先物化成 `Vec` 再并行。

**练习 2**：导出阶段 `Asset` 分支只做 `bytes.clone()`，看起来几乎无开销，为什么还要放进 `par_iter` 一起并行？

> **参考答案**：为了让 `Document` 和 `Asset` 在**同一个并行遍历**里统一处理、统一用 `collect_combined_result` 汇总成 `VirtualFs`。`Asset` 本身廉价，但放进同一并行流不必为它单独写一条串行路径，代码更简单；rayon 对极轻任务的调度开销也可忽略。

---

### 4.2 comemo 记忆化：`bundle_impl` 与四个导出函数

#### 4.2.1 概念说明

`#[comemo::memoize]` 把一个函数变成**按输入身份缓存**的纯函数：同样的输入返回缓存的输出，跳过整段计算。bundle 共有**五个**记忆化点：

| 函数 | 位置 | 缓存了什么 | 关键输入（缓存键的一部分） |
| --- | --- | --- | --- |
| `bundle_impl` | lib.rs | 整个 `Bundle`（含统一内省器） | `introspector: Tracked<dyn Introspector>` 等 |
| `export_pdf` | export.rs | PDF 字节 | `doc`、`anchors`、`link_resolver: Tracked<LateLinkResolver>` |
| `export_png` | export.rs | PNG 字节 | `doc`、`options` |
| `export_svg` | export.rs | SVG 字节 | `doc`、`anchors`、`link_resolver` |
| `export_html` | export.rs | HTML 字节 | `root`、`options`、`link_resolver` |

记忆化在 bundle 里有两层意义：

1. **性能**：内省迭代会反复调用 `bundle_impl`；一旦输入稳定，记忆化让后续调用直接命中缓存。导出函数同样遵循 Typst 全项目惯例——所有「产出输出」的函数都记忆化，便于增量复用。
2. **正确性/设计约束**：记忆化**倒逼**函数被设计成「输入决定输出」的纯函数。最典型的例子是 `export_html` 故意只接收根元素而非整个文档——下文详述。

#### 4.2.2 核心流程

`bundle_impl` 的记忆化与普通函数略有不同：它的输入里有几个 `Tracked`/`TrackedMut` 句柄（`world`、`introspector`、`traced`、`sink`、`route`）。comemo 用这些句柄的**身份**参与缓存键，而不是把整个 world/introspector 的内容哈希一遍。因此：

```text
bundle(engine, content, styles)
  └─ bundle_impl(world, library, introspector, traced, sink, route, content, styles)
        └─ #[comemo::memoize]
           若 (这些输入的身份/内容) 与上次相同 → 返回缓存的 Bundle
           否则 → 重新执行 realize/collect/parallelize/...，并缓存结果
```

四个 `export_*` 的形态更简单：纯输入 → `Bytes`，且都额外挂了 `#[typst_macros::time]` 计时宏。

#### 4.2.3 源码精读

**(1) `bundle_impl` 的记忆化**

[crates/typst-bundle/src/lib.rs:139-150](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L139-L150) — `#[comemo::memoize]` 标注在 `bundle_impl` 上；注意它的参数里 `introspector: Tracked<dyn Introspector + '_>`、`sink: TrackedMut<Sink>` 等都是 tracked 句柄。

外层 `bundle` 只是一个「解包 `Engine` 字段、转交 `bundle_impl`」的薄包装，本身**不**记忆化（它借用 `&mut Engine`，无法做按值缓存）：

[crates/typst-bundle/src/lib.rs:120-136](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L120-L136) — `bundle` 把 `engine.introspector.into_raw()`、`TrackedMut::reborrow_mut(&mut engine.sink)` 等传给 `bundle_impl`。

**(2) 四个导出函数的记忆化（及一个精心的边界选择）**

[crates/typst-bundle/src/export.rs:78-87](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L78-L87) — `export_pdf`，`#[comemo::memoize]`，调用 `typst_pdf::pdf_in_bundle`。

[crates/typst-bundle/src/export.rs:90-98](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L90-L98) — `export_png`，记忆化后直接复用 `typst_render::render`，**不**走 `_in_bundle` 变体（PNG 不支持链接）。

[crates/typst-bundle/src/export.rs:101-125](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L101-L125) — `export_svg`，记忆化，调 `typst_svg::svg_in_bundle`。

最值得读的是 `export_html` 的**文档注释**——它解释了一个「为记忆化而设计函数签名」的真实案例：

[crates/typst-bundle/src/export.rs:127-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L127-L142) — 注释大意：本函数接收 **root 元素**而非整个 `HtmlDocument`，是因为它不需要 metadata/introspector，且这样**才能被干净地记忆化**。HTML 文档在构建之后会被**原地改写**（用于注入链接），所以它不是 100% 由自身派生的纯数据；把 introspector 拖过记忆化边界比 paged 麻烦得多。于是作者选择只传 `root`，把「可变状态」挡在缓存边界之外。

**(3) `Tracked<LateLinkResolver>` 跨记忆化边界**

[crates/typst-bundle/src/export.rs:80-85](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L80-L85) — `link_resolver: Tracked<LateLinkResolver>` 作为参数。正是 u4-l2 强调过的：它以 tracked 身份（而非内容哈希）参与缓存键，所以可以廉价地穿越记忆化边界。

#### 4.2.4 代码实践

**实践目标**：定位五个记忆化点，并读懂「为记忆化而选签名」这一设计取舍。

**操作步骤**：

1. 在四个 `export_*` 与 `bundle_impl` 上各标注 `#[comemo::memoize]` 的行号（export.rs 的 78 / 90 / 101 / 134，lib.rs 的 139）。
2. 精读 [export.rs:127-133](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L127-L133) 的注释，对比 `export_html(doc.root(), ...)`（[export.rs:72](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L72)）与 paged 路径传入整个 `doc` 的区别。

**需要观察的现象**：

- `export_html` 的参数是 `root: &HtmlElement`，而 `export_pdf/svg` 的参数是 `doc: &PagedDocument`。
- 四个导出函数都同时挂了 `#[comemo::memoize]` 和 `#[typst_macros::time]`。

**预期结果**：你能解释「HTML 文档构建后会被原地改写，不是纯派生数据，所以 `export_html` 只把不可变的 root 元素送过记忆化边界，避免把可变 introspector 纳入缓存键」。

#### 4.2.5 小练习与答案

**练习 1**：`bundle` 函数本身没有 `#[comemo::memoize]`，但 `bundle_impl` 有。为什么要把记忆化下沉到 `bundle_impl`？

> **参考答案**：`bundle` 接收 `&mut Engine`，是借用的、不可按值哈希的；而 `bundle_impl` 把所需状态拆成一组**可跟踪、可按身份比较**的参数（`Tracked` 句柄 + `&Content` + `StyleChain`），满足 comemo 记忆化对「输入可作缓存键」的要求。下沉一层是为了让缓存键干净。

**练习 2**：`export_png` 没有 `link_resolver` 参数，而 PDF/SVG 有。这与记忆化有什么关系？

> **参考答案**：PNG 不支持链接，导出不需要解析跨文档链接，故无需 `link_resolver`（也就没有 `Tracked<LateLinkResolver>` 这个缓存键）。这同时简化了签名、缩小了缓存键维度——是「能力越少、记忆化越干净」的一个缩影。

---

### 4.3 设计取舍：并行编译 + 统一内省收敛

> 本节是本讲的核心，也是规格里要求的主实践任务所在。它回答一个问题：**为什么 `bundle_impl` 要「先并行编译各文档 → 再做一次统一的 `BundleIntrospector::new` + `set_anchors`」？这种顺序如何保证跨文档内省的正确性？**

#### 4.3.1 概念说明

bundle 最独特的设计是 u5-l1 讲过的「**一个统一内省器、一次大循环**」：文档 A 可以 `#link` 到文档 B、可以 `query` B 里的元素，反之亦然。这是一种**互相依赖、甚至循环依赖**的关系，无法一次性算完，只能**迭代到不动点**。

这就带来一个看似矛盾的要求：

- **要并行**：每个文档的排版很重，逐个串行太慢。
- **要一致**：文档 A 排版时对 B 的观察，必须和 B 排版时被观察的状态**来自同一份快照**，否则结果会随线程调度而变（数据竞争 / 不可复现）。

bundle 的解法是**把并行限制在「一轮迭代之内」，把「快照切换」放在「两轮迭代之间」**：

- 在一轮迭代里：所有文档**对着同一份冻结的内省快照**并行排版。谁也不看谁的「半成品」。
- 一轮结束时：把这一轮全部排完的文档**汇合**成一份新的统一内省器。
- 下一轮：用这份新内省器当新的冻结快照，重复。
- 收敛：当新内省器与上一轮的快照「观察上等价」时停止。

换句话说，并行负责「一轮之内快」，串行的 `BundleIntrospector::new` 负责「跨文档一致性」，记忆化（comemo）则负责「检测收敛 + 稳定后免重算」。

#### 4.3.2 核心流程

把 `bundle_impl` 放回顶层 `compile_impl` 的迭代循环里看，全貌如下：

```text
compile_impl::<Bundle>（crates/typst/src/lib.rs）
  loop {                                    // 内省不动点迭代
    introspector_n = 上一轮的 introspector（首轮为 EmptyIntrospector）
    constraint = comemo::Constraint::new()
    把 introspector_n 用 constraint 追踪，喂给 Engine
    document = Bundle::create(engine, content, styles)
        └─ bundle() ── bundle_impl()  ← #[comemo::memoize]
             ① realize + collect          （串行：结构校验、查重）
             ② parallelize 各文档排版      （并行：都只读 introspector_n 这份冻结快照）
             ③ BundleIntrospector::new    （串行：汇合本轮全部文档 → introspector_{n+1}）
             ④ link_targets + create_link_anchors + set_anchors（锚点回填）
             返回 Bundle{ files, introspector_{n+1} }
    if constraint.validate(introspector_{n+1}) {   // 收敛判定
        break
    }
    history.push(document)
  }
```

**为什么 ② 在 ③ 之前是正确性的关键**：

- ②并行排版时，每个子引擎共享的 `introspector` 字段（[engine.rs:66,80](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L66)）就是传入 `bundle_impl` 的那份 `introspector_n`。它在这一整轮里**只读、不变**。所以无论线程怎么调度、文档谁先排完，每个文档看到的「别的文档」都是 `introspector_n` 这同一份历史快照——**确定性得到保证**。
- ③在**全部文档排完之后**才构造新内省器。此刻每个文档都是完整成品，汇合出的 `introspector_{n+1}` 反映了本轮所有文档的完整状态。没有任何文档看到过另一个文档的「半成品」。
- ④的锚点（u5-l2）是导出期数据，依赖已构造好的内省器，且不回灌进内省迭代，所以放在 ③ 之后是安全的。

**收敛判定的数学含义**：设一轮迭代把快照 `I_n` 映射成新内省器 `I_{n+1} = F(I_n)`（`F` 就是「并行排版 + 汇合」）。`constraint.validate(I_{n+1})` 为真，等价于「在 ②中实际被读取的那些内省点上，`I_{n+1}` 与 `I_n` 一致」，即 `F(I_{n+1})` 会产出与 `F(I_n)` 相同的结果——不动点达成：

\[
\text{收敛} \;\Longleftrightarrow\; \texttt{constraint.validate}(I_{n+1}) = \text{true} \;\Longleftrightarrow\; F(I_{n+1}) = F(I_n).
\]

#### 4.3.3 源码精读

**(1) 顶层迭代循环与收敛判定**

[crates/typst/src/lib.rs:138-161](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L138-L161) — 每轮：取上一轮 introspector（[lib.rs:140-143](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L140-L143)）、新建 `Constraint` 并用它追踪该 introspector（[lib.rs:144,150](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L144-L150)）、调 `T::create`（即 `bundle` → `bundle_impl`）、最后 `constraint.validate(document.introspector())` 判收敛（[lib.rs:158](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L158)）。

> 注意 `MAX_ITERS` 上限（[lib.rs:163-182](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L163-L182)）：超过次数仍未收敛会放弃，并调用 `introspection::analyze` 给出诊断警告。

**(2) `bundle_impl` 内部的「并行 → 汇合 → 锚点」顺序**

[crates/typst-bundle/src/lib.rs:179-200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179-L200) — 这正是本节标题所问的顺序：

- [lib.rs:179-195](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179-L195)：`parallelize` **并行**编译各文档（只读冻结快照）。
- [lib.rs:197](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197)：`BundleIntrospector::new(&items)` **汇合**本轮全部文档，产出 `introspector_{n+1}`。
- [lib.rs:198-200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L198-L200)：`link_targets()` → `create_link_anchors` → `introspector.set_anchors(anchors)`，回填锚点。

**(3) 汇合点的实现：`BundleIntrospector::new` / `set_anchors`**

[crates/typst-bundle/src/introspect.rs:39-45](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L39-L45) — `new` 串行遍历所有 `items`，把每个文档的子内省器与元素合并进统一结构（详见 u5-l1）。

[crates/typst-bundle/src/introspect.rs:65-67](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L65-L67) — `set_anchors` 只是回填锚点表；锚点不影响内省迭代本身，故可放心地在收敛骨架之外完成。

**(4) 记忆化如何「兼作」收敛检测**

这一点容易被忽略：`bundle_impl` 标了 `#[comemo::memoize]`（[lib.rs:139](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L139)），而它的入参 `introspector` 正是用 `compile_impl` 那个 `constraint` 追踪过的同一个句柄。于是 ②中各文档读取内省器时，comemo 会把「读了哪些点」记录进 `constraint`；[lib.rs:158](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L158) 的 `constraint.validate(新 introspector)` 正是复用这份记录来回答「换上新内省器，结果会不会变」。**记忆化的约束机制，顺带就成了收敛检测器**——收敛后下一次 `bundle_impl` 还会直接命中缓存，几乎零成本返回。

> 这就是为什么说并行与记忆化在 bundle 里是「协同设计」而非「各自为政」：并行让每轮快，记忆化的约束让收敛可判、让稳定后免重算。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在源码里**标全**所有 rayon 并行点与 comemo 记忆化点，并写一段分析，论证「先并行编译 → 再统一 `BundleIntrospector::new` + `set_anchors`」如何保证跨文档内省的正确性。

**操作步骤**：

1. **标并行点（3 处）**：
   - 编译：[lib.rs:179](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179)（`engine.parallelize`）→ 内部 [engine.rs:75](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L75)（`into_par_iter`）。
   - 导出：[export.rs:27](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L27)（`par_iter`）。
   - 锚点：[link.rs:26](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L26)（`par_iter_mut`）。
2. **标记忆化点（5 处）**：[lib.rs:139](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L139)（`bundle_impl`）+ [export.rs:78](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L78) / [90](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L90) / [101](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L101) / [134](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L134)（四个 `export_*`）。
3. **追一次完整顺序**：从 [lib.rs:179](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179) 读到 [lib.rs:200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L200)，确认「parallelize → new → link_targets → create_link_anchors → set_anchors」的先后。
4. **写分析**（建议 200–400 字），至少覆盖：
   - 为什么并行排版期间各文档不会看到彼此的半成品（引用 `engine.rs` 中子引擎共享只读 `introspector`、各自独立 `sink`）；
   - 为什么必须等全部排完才能 `BundleIntrospector::new`（汇合出一致快照）；
   - `set_anchors` 为什么放在最后且不影响收敛；
   - `constraint.validate` 如何用记忆化记录的约束判定收敛。

**需要观察的现象**：

- `parallelize` 的子引擎构造（[engine.rs:78-85](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L78-L85)）里，`introspector` 取自解构的主引擎（只读），`sink` 是新建的——这就是「同一份冻结快照 + 独立错误收集」。
- `BundleIntrospector::new` 接收的是 `&[Item]`，即**已经全部排完**的文档集合。

**预期结果**：你能给出类似下面的论断——

> 「并行只发生在单轮迭代内部，且所有子任务读取的是同一份只读的历史内省快照，因此结果与线程调度无关、可复现；只有当全部文档排版完成，才把它们汇合成新一轮的统一内省器作为下一轮快照；锚点是导出期产物，不回灌迭代，故置于汇合之后。收敛由 comemo 的 `constraint.validate` 判定：它在并行排版中记录了实际读取的内省点，若新内省器在这些点上与旧快照一致即视为不动点。」

**可选的运行验证（待本地验证）**：若你已启用 `--features bundle`，可写一个两文档互相 `#link` 的最小 bundle，在开启计时（如设置 Typst 的 timings 输出）时编译，观察「introspect / layout bundle」相关的计时条目在收敛前会出现多于一次、收敛后稳定——以此直观感受「多轮迭代」。本步骤依赖具体 CLI 版本的计时开关，若无法复现请标注「待本地验证」，不要伪造输出。

#### 4.3.5 小练习与答案

**练习 1**：假设把 `BundleIntrospector::new(&items)` 移到 `parallelize` **之前**（即先构造内省器再排版），会出什么问题？

> **参考答案**：排版尚未发生，`items` 里还没有任何已排版的文档，构造出来的内省器是空的/旧的，各文档排版时拿不到本轮彼此的信息；更糟的是，若让排版去读写一个「正在被构造」的内省器，会破坏「同一份冻结快照」前提，使结果依赖线程调度、不可复现。正确做法只能是「先排完 → 再汇合」。

**练习 2**：为什么说 bundle 是「在迭代之内并行、在迭代之间串行切换快照」？

> **参考答案**：一轮迭代内，`parallelize` 让各文档并行读取同一份冻结快照 `I_n`（并行）；一轮结束后，串行地用 `BundleIntrospector::new` 汇合出 `I_{n+1}` 作为下一轮快照（串行切换）。并行负责单轮提速，串行的快照切换负责跨文档一致性与可收敛性。

**练习 3**：`constraint.validate` 与 `#[comemo::memoize]` 是同一个机制吗？

> **参考答案**：不是同一个，但都出自 comemo、且在 bundle 里协同。`#[comemo::memoize]` 按**输入身份**缓存结果；`Constraint` + `track_with` + `validate` 记录**读取了输入的哪些部分**，用于反问「换新值结果会不会变」。前者让稳定后免重算，后者直接充当收敛检测器。二者共同支撑了不动点迭代。

## 5. 综合实践

把本讲三块知识串起来，做一次「**性能机制全景走查**」：

1. **画一张流水线时序图**：横轴是时间/阶段，依次画出 `realize+collect`（串行）→ `parallelize` 编译（并行）→ `BundleIntrospector::new`（串行汇合）→ `create_link_anchors`（并行）→ `export`（并行）。在每个阶段上标注它用到的 rayon API（`into_par_iter` / `par_iter` / `par_iter_mut`）或「串行」。
2. **在图上叠一层记忆化**：标出 `bundle_impl` 覆盖前四个阶段（整段被记忆化），四个 `export_*` 覆盖最后的导出阶段（各自记忆化）。
3. **标注收敛环**：在外层用一个大箭头从「汇合出的 introspector」指回下一轮的「冻结快照」输入，并注明判定点 `constraint.validate`。
4. **写一段总结**：用本讲的术语解释——为什么这套「并行 + 记忆化 + 统一内省」的组合，能让「多个文档互相内省」这件事既快、又正确、又必定收敛（或在 `MAX_ITERS` 内给出诊断）。

完成后，你应当能用一张图向别人讲清 bundle 的性能骨架。

## 6. 本讲小结

- bundle 有 **3 个 rayon 并行点**：编译（`engine.parallelize` → `into_par_iter`）、导出（`par_iter`）、锚点生成（`par_iter_mut`），都作用在「逐文件独立」的工作上。
- `Engine::parallelize` 用「**每个子任务一个独立 `Sink`、事后串行合并**」安全收集并行中的错误/延迟错误/警告；`collect_combined_result` 系列**不短路**，先跑完再汇总全部错误。
- bundle 有 **5 个 comemo 记忆化点**：`bundle_impl` + 四个 `export_*`；记忆化既提速，也**倒逼**函数写成「输入决定输出」的纯函数（典型案例：`export_html` 只传 root 元素以避开可变 introspector）。
- **正确性核心**：并行被限制在「一轮迭代之内」，所有文档读取同一份**冻结的历史内省快照**；只有全部排完，才用 `BundleIntrospector::new` 汇合成新快照——这保证了结果与线程调度无关、可复现。
- **收敛核心**：不动点迭代 `I_{n+1}=F(I_n)`，由 comemo 的 `constraint.validate` 判定；记忆化的约束记录顺带充当收敛检测器，稳定后 `bundle_impl` 命中缓存近乎零成本。
- 「并行编译 vs 统一内省收敛」不是对立，而是**分工**：并行管单轮提速，串行的快照切换管跨文档一致与可收敛性，记忆化把二者粘合。

## 7. 下一步学习建议

- 下一讲 **u5-l4「CLI 集成与端到端实践」** 会把这些内部机制接到用户可见的入口：`typst-cli` 如何用 `compile::<Bundle>` 驱动整个流水线、`export_bundle` 如何构造 `BundleOptions` 并把 `VirtualFs` 落盘。学完那一讲，本讲的并行/记忆化点就有了「从命令行到字节」的完整落点。
- 若想深挖本讲两个底层依赖，建议阅读：`crates/typst-library/src/engine.rs`（`parallelize` 与 `Sink` 的完整实现）、`crates/typst/src/lib.rs` 的 `compile_impl`（迭代循环与 `MAX_ITERS` 诊断）、以及 comemo 文档中关于 `Constraint` / `track_with` / `validate` 的说明，亲手验证「约束如何等价于收敛判定」。
- 进阶思考题（留给读者）：如果 bundle 未来要支持「文档间真正的流式依赖」（而非快照式不动点），现有的「并行 + 记忆化」骨架需要做哪些改动？带着这个问题重读 4.3 会很有收获。
