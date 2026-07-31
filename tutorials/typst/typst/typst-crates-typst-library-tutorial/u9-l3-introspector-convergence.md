# Introspector 与收敛循环

## 1. 本讲目标

本讲是「内省与上下文」单元的收口篇。前两讲（u9-l1、u9-l2）我们已经知道：用户代码通过 `query`/`locate`/`here`、`Counter`、`State`、`Metadata` 这些入口，去读取「文档排完之后才能知道的信息」（比如某个标题在第几页、某计数器当前的值）。但每次排版时，这些信息还**没排完**，只能去读**上一轮**排好的结果。这就引出本讲要回答的三个问题：

1. **Introspector 是什么？** 它是每轮排版结束后建好的「文档索引」，提供统一的查询接口。
2. **Typst 怎么知道内省结果已经稳定了？** 靠 comemo 的记忆化与验证——这是收敛循环的「快路径」。
3. **如果一直稳定不下来怎么办？** 最多迭代 5 次（`MAX_ITERS`），之后用 `convergence.rs` 的 `analyze` 产出「document did not converge」诊断。

学完本讲，你应当能：

- 说清 `Introspector` trait 的统一查询接口与 `ElementIntrospector` 的加速结构。
- 解释 comemo 的 `Constraint::validate` 如何充当收敛判定的快路径。
- 描述收敛循环的迭代次数上限、`History` 的历史比对机制，以及 comemo 验证失败与「真正未收敛」之间的细微差别。

## 2. 前置知识

本讲假设你已掌握前两讲的内容：

- **u9-l1**：`Location`（u128 哈希「身份证号」）、`Tag`/`TagElem`（把位置盖章进帧树）、`Locator`/`SplitLocator`（分层哈希发号机）、`query`/`locate`/`here` 三个用户函数都「先过 `Context` 门禁，再走 `engine.introspect(...)` 查 Introspector」，以及核心结论——**第 N 轮排版时用户代码查到的是第 N-1 轮建好的索引**。
- **u9-l2**：`Counter`/`State` 用 `sequence_impl`（标 `#[comemo::memoize]`）把整条轨迹一次算完、用 `query_count_before` 按位置取偏移；统一模型是「插入不可见 locatable 元素 → 内省器索引 → `engine.introspect` 读上一轮的值 → 收敛循环验证」。
- **u5-l2**：`Engine` 是随求值/收敛/排版一路传递的「手提箱」；`Sink` 有四只桶，其中 `introspections` 桶专门收集本轮做过的内省，`delayed` 桶收集延迟错误，**只有撑到收敛循环结束仍存在的延迟错误才被提升为致命错误**。

此外需要两个外部概念：

- **comemo**：Typst 自研的增量记忆化（memoization）库。给 trait 或 impl 标上 `#[comemo::track]` 后，通过 `Tracked<dyn Trait>` 调用的方法会被自动缓存：相同输入直接返回缓存，输入变了缓存自动失效。这是 Typst 增量编译的根基。
- **不动点（fixed point）**：内省读「上一轮的输出」，而这一轮的输出又依赖这一轮读到的值——这是一个「输出依赖自身」的递推。反复迭代直到连续两轮输出相同，就叫「收敛到不动点」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/introspection/introspector.rs` | 定义 `Introspector` trait（统一查询接口）、空实现 `EmptyIntrospector`、通用实现 `ElementIntrospector<P>` 及其构造器 `ElementIntrospectorBuilder`、查询缓存 `QueryCache`。 |
| `src/introspection/convergence.rs` | 定义收敛分析入口 `analyze`、`Introspect` trait（带 `Output`/`introspect`/`diagnose`）、类型擦除的 `Introspection`、以及跨迭代比对历史值的 `History`。 |
| `src/engine.rs` | `Engine::introspect` 在执行内省的同时把内省记进 `Sink`；`Sink` 的 `introspection` 方法负责收集。 |
| `src/introspection/query.rs` | `QueryIntrospection` 是 `Introspect` trait 的典型实现，`query` 用户函数经 `engine.introspect(QueryIntrospection(...))` 走完整链路。 |
| `src/introspection/counter.rs` | `CounterAtIntrospection` 等是「为诊断量身定制」的自定义内省，展示 `diagnose` 如何被精细化。 |
| `crates/typst/src/lib.rs`（顶层 `typst` crate） | `compile_impl` 里的 `loop` 就是收敛循环的真正驱动：建约束、排版、`constraint.validate` 判稳、耗尽次数则调 `analyze`。 |

> ⚠️ 注意：收敛循环的**驱动代码不在 `typst-library`**，而在顶层 `typst` crate（`crates/typst/src/lib.rs`）。本 crate 只提供 `Introspector`、`Introspect`、`analyze` 这些「零件」，由顶层 crate 拼成循环。这与 u1-l1 讲的「类型留本 crate、行为在别处」一脉相承。

## 4. 核心概念与源码讲解

### 4.1 Introspector trait：统一查询接口

#### 4.1.1 概念说明

每一轮排版结束后，Typst 会从排好的文档里**采集所有「可内省」的元素**（带 `Locatable` 能力、被 `Tag` 盖了章的元素，见 u9-l1），连同它们的位置信息一起，建成一张**索引**。这张索引就是 `Introspector`。

`Introspector` 是一个 trait，它把「向文档提问」的所有方式抽象成一组统一方法：查全部匹配、查第一个、查唯一、按标签查、统计某位置之前有几个匹配、查页码、查坐标……无论 paged 还是 html 目标，标准库代码只对着这个 trait 编程，不关心索引内部长什么样。不同输出目标各有自己的实现，某些方法在特定目标下可能返回 `None`（例如 html 目标的 `page` 查询）。

#### 4.1.2 核心流程

一轮排版中，`Introspector` 的生命周期是：

1. 排版器在排出 `Frame` 帧树时，遇到 `Tag::Start`/`Tag::End` 就调 `ElementIntrospectorBuilder::discover_tag`，把可内省元素及其位置**按文档顺序**塞进构造器。
2. 排版结束后 `finalize` 把构造器整理成 `ElementIntrospector`——一个带若干加速结构（按 location、按 label、按 key 哈希）的查询表。
3. 下一轮排版时，用户代码经 `engine.introspect(...)` 拿到 `Tracked<dyn Introspector>`，调用其方法查询；查询结果会被 comemo 记忆化（见 4.2）。
4. 第一轮排版时还没有任何「上一轮」，于是用一个永远返回空结果的 `EmptyIntrospector` 顶替。

查询分发的伪代码（以最常用的 `query` 为例）：

```
query(selector):
    若 selector 的哈希已在 QueryCache 命中 → 直接返回缓存
    否则按 selector 的种类分发：
        Elem(..)        → 遍历全部元素，逐个 matches 过滤
        Location(loc)   → 按 location 直接取
        Label(label)    → 按 label 索引取
        Or / And        → 递归 query 各子选择器再并集 / 交集
        Before / After  → 递归 query 后用 binary_search 切片
        Within          → 递归 query，按祖先的 index 区间收纳后代
    把结果按 selector 哈希写入 QueryCache，返回
```

#### 4.1.3 源码精读

**trait 定义**——注意 `#[comemo::track]`，这让所有通过 `Tracked<dyn Introspector>` 的调用都变成可记忆化的（4.2 会展开）：

[src/introspection/introspector.rs:28-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L28-L89) — `Introspector` trait，标了 `#[comemo::track]`，含 `query`/`query_first`/`query_unique`/`query_label`/`query_count_before`/`page`/`position` 等十几个统一查询方法。

其中几个关键方法的语义：

- `query_count_before(selector, end)`（[L47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L45-L47)）是 `query(selector.before(end, true)).len()` 的优化版，被 `Counter`/`State` 高频调用（见 u9-l2 的 `sequence_impl`），所以专门做成一个方法。
- `locator(key, base)`（[L58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L52-L58)）用于「测量模式」下，在已知位置附近找一个最接近的真实元素位置（见 u9-l1「Dealing with Measurement」）。

**空实现**——第一轮排版没有上一轮的文档，用它顶替，所有查询都返回空：

[src/introspection/introspector.rs:100-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L100-L164) — `EmptyIntrospector`：`query` 返回空数组、`query_first` 返回 `None`、`page` 返回 `None`……这正是「第一轮内省结果全是空」的根源，所以只要文档里有内省，几乎必然需要第二轮。

**通用实现与加速结构**：

[src/introspection/introspector.rs:169-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L169-L190) — `ElementIntrospector<P>` 的字段：`elems`（元素+位置，按文档顺序）、`keys`（按 key 哈希→location，服务于测量定位）、`locations`（按 location→index 区间，加速按位置取元素及其后代）、`labels`（按 label→index）、`queries`（`QueryCache` 查询缓存）。注释指出缓存很重要，因为各顶层查询常共享子查询（例如多个带 `before` 的计数器查询都依赖同一个全局计数器查询）。

[src/introspection/introspector.rs:194-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L194-L342) — `ElementIntrospector::query`：开头先查 `QueryCache`（L195-L198），命中则直接返回；否则按 selector 种类分发；最后把结果写回缓存（L340-L341）。`Before`/`After`/`Within` 这些需要文档顺序的选择器，都靠 `binary_search` 在已排序的元素列表上做切片（L246-L335）。

[src/introspection/introspector.rs:686-703](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L686-L703) — `QueryCache` 用 `RwLock<FxHashMap<u128, EcoVec<Content>>>`，键是 selector 的 128 位哈希，允许多读单写。

#### 4.1.4 代码实践

**实践目标**：理解「第一轮内省结果为空」如何驱动至少两轮排版。

**操作步骤**：

1. 新建 `hello.typ`：

   ```typ
   #set page(numbering: "1")
   = 第一章 <a>
   #context [第一章在第 #locate(<a>).page() 页]
   #pagebreak()
   = 第二章
   ```

2. 用 Typst CLI 编译：`typst compile hello.typ`（若本地无 CLI，标注「待本地验证」）。

3. 阅读源码：对照 [introspector.rs:100-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L100-L164) 的 `EmptyIntrospector`，思考第一轮排版时 `locate(<a>).page()` 会拿到什么（`None`），从而页码推导会与第二轮不同。

**需要观察的现象**：最终 PDF 里「第一章在第 1 页」应正确显示——说明第二轮（及以后）用到了第一轮建好的真实索引，结果稳定下来。

**预期结果**：文档正常输出，无收敛警告。这正是因为收敛循环（4.3）在 2 轮内就达到了不动点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ElementIntrospector` 要同时维护 `locations`（按 location）和 `labels`（按 label）两套索引，而不是只遍历 `elems`？

**参考答案**：遍历 `elems` 是 O(n)，而内省查询（尤其计数器的 `query_count_before`）在单次排版里会被高频、重复地调用。`locations`/`labels` 把按位置/按标签的查找降到接近 O(1)，再配合 `QueryCache` 把整条查询的结果缓存起来，避免重复计算。

**练习 2**：`Selector::Can(_)` 和 `Selector::Regex(_)` 在 `ElementIntrospector::query` 里返回空（[L337](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L336-L337)），为什么？

**参考答案**：这两种选择器需要「文档位置」才能判定匹配（能力查询、正则匹配都依赖运行期上下文），而 `ElementIntrospector::query` 是位置无关的纯索引查询。它们在 u4-l2 讲过会返回 false 留给 `query`（内省）阶段处理，所以这里直接返回空，交由上层（行为 crate）另行处理。

---

### 4.2 comemo 记忆化与验证：收敛判定的快路径

#### 4.2.1 概念说明

comemo 给 Typst 提供了两样东西：

1. **记忆化（memoization）**：标了 `#[comemo::track]` 的 trait（如 `World`、`Introspector`、`Sink`、`Traced`、`Route`），通过 `Tracked<dyn Trait>` 调用时会自动缓存。缓存键包含「这次调用读了哪些被追踪的输入」。如果输入没变，下次直接返回缓存；输入变了，缓存自动失效重算。这就是增量编译的基础。
2. **约束验证（constraint validation）**：一轮排版开始时建一个 `comemo::Constraint`，排版过程中所有记忆化调用都把「读了哪个 introspector 的哪些值」记进这个约束；排完后用**这一轮新产出的 introspector** 去验证约束——「如果换成这个新 introspector，刚才那些缓存调用还会得到一样的结果吗？」

验证通过 ⇒ 这一轮的内省输入与上一轮一致 ⇒ 输出不会再变 ⇒ **收敛**。验证失败 ⇒ 某些被依赖的值变了 ⇒ 必须再排一轮。这是收敛循环的**快路径**：绝大多数文档在 1～2 轮内就通过验证停下来。

#### 4.2.2 核心流程

把 comemo 验证放进收敛循环里看（驱动代码在顶层 crate，4.3 详读）：

```
loop:
    constraint = comemo::Constraint::new()
    introspector = 上一轮文档的 introspector（第一轮用 EmptyIntrospector）
    engine.introspector = introspector.track_with(constraint)   # 绑定约束
    document = 排版(content, engine)                              # 期间的记忆化调用都记进 constraint
    if constraint.validate(document.introspector()):             # 用新 introspector 验证
        break                                                    # 收敛！
    if 已达上限: 调 analyze 后 break                            # 慢路径（4.3）
    history.push(document)                                       # 否则再来一轮
```

关键点：`track_with(&constraint)` 把「这一轮 introspector」与约束绑定；排版里所有 `introspector.query(...)` 这种被追踪调用，都会让 constraint 记下「我读过这个 introspector 的 query 方法，参数是某个 selector」。排版产出新文档后，`document.introspector()` 是**新索引**；`validate` 用新索引去回放这些调用，若结果全都不变即判稳。

#### 4.2.3 源码精读

驱动代码（顶层 `typst` crate，不在 typst-library）：

[crates/typst/src/lib.rs:144-161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L144-L161) — 每轮建 `comemo::Constraint::new()`，把 introspector 经 `track_with(&constraint)` 绑定进 `Engine`，排版后用 `constraint.validate(document.introspector())` 判稳；通过则 `break`。

注意 `Engine` 如何持有 introspector（本 crate）：

[src/engine.rs:28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L28) — `introspector: Protected<Tracked<'a, dyn Introspector + 'a>>`，即「被追踪的 Introspector 句柄」。`Protected` 是个阻止直接拿 `&mut` 的小包装，强制走 `access(...)` 才能取出（见 4.3.3 中 `Engine::introspect` 的用法）。

comemo 验证与「内省记录」是两件**正交**的事：

- **comemo 验证**：自动的、底层的、按记忆化调用粒度判定「整体有没有变」。它是布尔判定（通过/不通过），快但不告诉用户「哪里没收敛」。
- **Introspection 记录**（下一节详述）：手动的、显式的，把「这一次具体内省的输入与输出」存进 `Sink`，**专门用于在失败时生成可读诊断**。

这正是为什么两者并存：comemo 负责「快」，`Introspection`/`analyze` 负责「出问题时说清楚」。

#### 4.2.4 代码实践

**实践目标**：理解 comemo 验证是「快路径」，且它判定的是「记忆化调用结果有没有变」而非「文档内容有没有变」。

**操作步骤**：

1. 阅读 [crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L156-L161) 这一行 `if constraint.validate(document.introspector()) { ... break }`，用自己的话写下：它比较的是「这一轮读到的 introspector」与「上一轮读到并缓存了的 introspector」之间，被依赖的那些查询结果是否一致。
2. 构造一个**只换皮、不换查询结果**的文档（例如 `#context { query(heading); let _ = "装饰用文本" }`，多次编译只改装饰文本），推测：尽管文档字节变了，但因为内省查询的结果没变，comemo 验证应仍可能通过。

**需要观察的现象 / 预期结果**：理解为什么「comemo 验证通过」不等价于「两轮文档逐字节相同」，而是「所有被内省读取过的值相同」。若无法本地运行，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 comemo 验证比「逐字段比较两轮文档」更便宜、更适合做收敛判定？

**参考答案**：它只关心「被记忆化调用实际读取过的值」，未触及的部分（例如纯样式装饰）即使变了也不影响内省结果，无需触发重排。同时它复用了已有的缓存结构，无需额外构造一份文档副本来做全量比对。

**练习 2**：`Engine.introspector` 字段类型是 `Protected<Tracked<...>>`，而不是直接 `&dyn Introspector`。结合 4.3 的 `Engine::introspect`，说说 `Protected` 的作用。

**参考答案**：`Protected` 阻止代码直接拿到 introspector 句柄去「偷偷查询而不记录」。要查询必须走 `Engine::introspect`，它取出句柄后会顺手把这次内省记进 `Sink`（见 [engine.rs:113-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L109-L117)），从而保证失败时 `analyze` 有历史可查。

---

### 4.3 convergence 收敛分析：迭代上限与「未收敛」诊断

#### 4.3.1 概念说明

内省是一个**不动点问题**：第 N 轮的内省结果依赖第 N-1 轮的文档，而第 N 轮的文档又依赖第 N 轮的内省结果。Typst 的策略是「反复排版直到连续两轮一致」——但**不能无限迭代**，否则一个自指的查询（比如「查到几个标题就生成几个标题」）会把编译拖死。

于是 `convergence.rs` 定义了硬上限 `MAX_ITERS = 5`：最多排 5 轮，仍不稳定就放弃并产出诊断。这里的诊断不是 comemo 给的（comemo 只说「没通过」），而是靠这一路排版中显式记录的 `Introspection` 重放历史、逐条判定「哪一次内省的输出值在最后两轮之间发生了变化」，进而给出可读的「document did not converge」警告。

`Introspect` trait 是这套机制的抽象核心：每一种内省（query、counter、state、position……）都实现它，声明自己的 `Output`（应当稳定的值）、`introspect`（怎么取值）、`diagnose`（不稳定时怎么报错）。

#### 4.3.2 核心流程

完整收敛循环（结合驱动代码与 `analyze`）：

```
history = []  # 容量 MAX_ITERS-1 = 4，存最近 4 轮文档
loop:
    introspector = history.last 或 EmptyIntrospector
    constraint = Constraint::new()
    document = 排版(introspector, constraint)
    if constraint.validate(document.introspector): 收敛 → break      # 快路径
    if history 已满(4):
        introspectors = [Empty, D1, D2, D3, D4, D5]   # 共 6 = MAX_ITERS+1 个
        warnings = analyze(world, introspectors, sink.introspections())
        把 warnings 灌进 sink → break                  # 慢路径
    history.push(document)
```

`analyze` 内部对每一条记录过的 `Introspection`：

1. 用 6 个历史 introspector 各跑一遍 `introspect`，得到 `History`（6 个输出值）。
2. 比对**最后两个**输出值的 128 位哈希是否相等（`converged`）。
3. 不相等 ⇒ 调该内省自己的 `diagnose` 生成一条诊断；相等 ⇒ 该内省其实稳定了，不报。

最后，只要有任何一条诊断，就在最前面追加一条汇总警告「document did not converge within five attempts」，并附 hint 指向 `https://typst.app/help/convergence`。

> **一个微妙之处**（源码注释花了一大段解释）：comemo 验证可能失败、但文档其实已经收敛了。例如用户把一次 query 的结果**归约成一个布尔值**——comemo 仍能看到底层 `EcoVec<Content>` 在变，于是验证失败、走到 `analyze`；但 `analyze` 比对的是归约后的布尔 `Output`，发现它已稳定，于是产出**零条诊断**，也就不发收敛警告。结论：**`analyze` 是「是否真未收敛」的最终裁判，comemo 只是触发它的快路径。**

判定收敛为什么用哈希比较？源码注释举了个例子：状态从 `0.0`（float）变成 `0`（int），虽然在 Typst 里 `0.0 == 0`（见 u2-l1 的相等兜底），但下一轮能观察到类型差异，**不应算作收敛**。用 128 位哈希比「完全相同」正好排除这种情况。

#### 4.3.3 源码精读

**迭代上限与实例数**：

[src/introspection/convergence.rs:16-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L16-L20) — `MAX_ITERS = 5`、`ITER_NAMES = ["iter (1)".."iter (5)"]`（用于计时输出）、`INSTANCES = MAX_ITERS + 1 = 6`（历史比对的 introspector 个数：空 + 4 个历史 + 1 个最终）。

**`analyze` 入口**：

[src/introspection/convergence.rs:24-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L24-L70) — 遍历所有记录的内省，各自 `diagnose`（L31-L35）；若产生过诊断，就在最前面插入汇总警告（L56-L67）。其中 [L37-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L37-L55) 那段注释正是上面讲的「comemo 失败但实际收敛 ⇒ 零诊断 ⇒ 不警告」的微妙逻辑。

**`Introspect` trait**：

[src/introspection/convergence.rs:93-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L93-L140) — 三个要素：
- `type Output: Hash`（[L125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L125)）：应当稳定的值。注释特别提醒，`Output` 装多少信息会影响收敛——把 query 归约成布尔可能比原始 query **早一轮**收敛。
- `introspect(&self, engine, introspector) -> Output`（[L131-L135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L131-L135)）：取值，主要用 introspector，但也会用到 engine（计数器要调用户函数）。
- `diagnose(&self, history) -> SourceDiagnostic`（[L139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L137-L139)）：不稳定时如何报错。

**类型擦除与历史比对**：

[src/introspection/convergence.rs:146-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L146-L156) — `Introspection(Arc<dyn Bounds>)`：把强类型的具体内省擦除成一个可存进 `Sink` 的对象。`Bounds` trait（[L164-L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L164-L172)）给所有 `T: Introspect` 提供 blanket impl（[L174-L187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L174-L187)）：跑历史、判收敛、必要时报错。

[src/introspection/convergence.rs:217-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L217-L236) — `History::compute`：为 6 个历史 introspector 各搭一个临时 `Engine`，跑一遍 `introspect` 收集 6 个输出值。

[src/introspection/convergence.rs:240-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L240-L250) — `History::converged`：比较 `self.0[MAX_ITERS-1]`（第 5 轮，即 D4 的输出）与 `self.0[MAX_ITERS]`（最终，即 D5 的输出）的 128 位哈希。注释说明为何用哈希而非 `==`。

[src/introspection/convergence.rs:268-280](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L268-L280) — `History::hint`：把 6 次取值格式化成「run 1…run 5…final」的提示文本，附在诊断上帮助用户看到「值在反复跳变」。

**两个真实 `Introspect` 实现对照**：

[src/introspection/query.rs:180-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L180-L203) — `QueryIntrospection`：`Output = EcoVec<Content>`，`introspect` 直接调 `introspector.query(&self.0)`，`diagnose` 区分「元素数量没稳」还是「查询本身没稳」两种措辞。`query` 用户函数走的就是 [query.rs:174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L174) 的 `engine.introspect(QueryIntrospection(target.0, span))`。

[src/introspection/counter.rs:787-814](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L787-L814) — `CounterAtIntrospection`：`Output = SourceResult<CounterState>`，`introspect` 里要调 `sequence`（吃 engine，因为可能要执行用户给的 numbering 函数）、`query_count_before`，并对页码计数器做 `step` 修正；`diagnose` 走专用的 `format_convergence_warning`，措辞更贴近用户对「计数器」的心智模型。这正是 trait 文档说的「计数器/状态本可复用 `QueryIntrospection`，但为了诊断更清晰而定制」。

**内省如何被记录（连接到 `Sink`）**：

[src/engine.rs:109-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L109-L117) — `Engine::introspect`：取出 introspector（绕过 `Protected`，因为「我们正要记录它」）、执行内省、把 `Introspection::new(introspection)` 推进 `Sink`。这是「执行 + 记录」的原子动作。

[src/engine.rs:207-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L207-L209) — `Sink::introspection`：往 `introspections` 桶里 push（桶定义见 u5-l2）。

**收敛循环驱动（顶层 crate）**：

[crates/typst/src/lib.rs:133-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L133-L185) — 完整 `loop`：[L158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L158) 的 `constraint.validate` 是快路径判稳；[L163-L182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L163-L182) 是慢路径——`history` 满后拼出 6 个 introspector（`[Empty, D1, D2, D3, D4, D5]`），调本 crate 的 `analyze`。循环结束后 [L188-L191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L187-L191) 才把延迟错误提升为致命错误——这正是 u5-l2 讲的「撑到收敛循环结束」。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：亲手复现「永不收敛」的文档，理解 `MAX_ITERS` 与 `analyze` 的关系。

**操作步骤**：

1. 新建 `bad.typ`，内容就是 `query.rs` 文档注释里那个经典自指示例子（[query.rs:91-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L91-L101)）：

   ```typ
   = Real
   #context {
     let elems = query(heading)
     let count = elems.len()
     count * [= Fake]
   }
   ```

2. 编译 `typst compile bad.typ`（若无 CLI，标注「待本地验证」）。
3. 同时阅读 [convergence.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L16-L18) 的 `MAX_ITERS` 与 [convergence.rs:24-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L24-L70) 的 `analyze`。

**需要观察的现象**：编译会输出一个**有限**数量的 `= Fake` 标题（因为 Typst 在 5 轮后放弃），并附带一条「document did not converge within five attempts」警告，其 hint 列出了每一轮 `query(heading)` 观察到的标题数量（1, 2, 3, 4, …）。

**预期结果**：你应能用一句话解释「Typst 为何需要反复编译直到内省稳定」——因为内省读的是上一轮的索引，自指查询会让每轮的索引都变，形成无限递推；`MAX_ITERS` 是兜底止损。再回答「comemo 验证通过与否和收敛诊断的关系」——comemo `validate` 是判定「要不要再来一轮」的快路径布尔开关；只有连续 5 轮都没通过、走到 `analyze` 时，才由 `analyze` 重放每条 `Introspection` 的 `History` 来判定「是否真的未收敛」并据此决定发不发警告。若两者结论不一致（comemo 失败但 `Output` 已稳定），以 `analyze` 为准、不发警告。

> 若无法本地运行 CLI，以上为「源码阅读型实践」：跟踪 [lib.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L156-L185) 的循环与 [convergence.rs:240-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L240-L250) 的 `converged`，口述上述数据流即可。

#### 4.3.5 小练习与答案

**练习 1**：`History` 里存了 6 个值（`INSTANCES = 6`），但 `converged` 只比较最后两个（`MAX_ITERS-1` 与 `MAX_ITERS`）。为什么不比较「第 1 个和最后 1 个」来判断是否曾稳定？

**参考答案**：收敛关心的是「连续两轮是否一致」，即不动点是否已经达到，而不是「是否曾经回到起点」。自指查询的值可能单调增长（1,2,3,4,5…），首尾不同恰恰说明没收敛；而一个真正收敛的文档，最后两轮必然相同。比较相邻两轮才是正确的不动点判据。

**练习 2**：假设你写了一个内省，`Output` 是「query 结果是否非空」的布尔值，而底层 `EcoVec<Content>` 每轮都在变（但始终非空）。comemo 验证会通过吗？`analyze` 会发警告吗？

**参考答案**：comemo 验证**不会通过**——它观察到的是底层 `EcoVec<Content>` 在变，于是每轮都判定「未稳定」、继续迭代，直到 5 轮上限走到 `analyze`。但 `analyze` 重放的是布尔 `Output`，发现「true,true,…,true」最后两个相等 ⇒ `converged` ⇒ 该内省不报错；若所有内省都如此，则零诊断、**不发收敛警告**。这正是 [convergence.rs:37-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L37-L55) 注释描述的情形。

**练习 3**：为什么 `Sink` 把内省记进 `introspections` 桶、而把 show 规则的早期报错记进 `delayed` 桶，两者都要「撑到收敛循环结束」才分别处理？

**参考答案**：两者都依赖「内省是否最终稳定」。内省记录只有在确定未收敛时才需要转化成诊断；延迟错误只有在最后一轮仍然存在时才是真错误（早期可能是 introspector 没就绪导致的假错误）。把它们都推迟到循环结束（[lib.rs:171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L171-L191) 的 `analyze` 与提升延迟错误）一并处理，才能过滤掉假象、只留下真正的问题。

## 5. 综合实践

把本讲三块知识串起来，完成一次「收敛循环全链路追踪」。

**任务**：给定下面这个会触发多轮排版的文档，逐轮推演 `Introspector` 的内容、comemo 验证的结果、以及 `analyze` 是否会被调用。

```typ
#set page(numbering: "1")
#heading[ Intro ] <h>
#context [ Intro 在第 #locate(<h>).page() 页 ]
#pagebreak()
#heading[ Next ]
```

**要求**：

1. **第 1 轮**：introspector 是 `EmptyIntrospector`（[introspector.rs:100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L100-L164)）。写出 `locate(<h>).page()` 这一会返回什么，以及因此「Intro 在第 ? 页」会渲染成什么。
2. **第 2 轮**：introspector 是第 1 轮文档建出的真实索引（含两个 heading、两页）。写出这次 `page()` 的返回值，并判断 `constraint.validate`（[lib.rs:158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L144-L161)）是否会通过。
3. **回答**：`analyze`（[convergence.rs:25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L25-L70)）会被调用吗？`PositionIntrospection`（见 [location.rs:171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L171)）的 `Output` 在最后两轮是否相等？

**参考要点**：第 1 轮 `page()` 返回 `None`（页码推导退化为默认/1），第 2 轮返回 `1`；由于第 2 轮用到的内省结果与第 2 轮文档自洽，`validate` 通过、循环在第 2 轮 `break`，`analyze` **不会**被调用，无任何收敛警告。

## 6. 本讲小结

- **`Introspector`** 是每轮排版后建好的「文档索引」，用 `#[comemo::track]` trait 提供统一查询接口；第一轮用 `EmptyIntrospector`（全空）顶替，`ElementIntrospector` 用 `locations`/`labels`/`keys`/`QueryCache` 多套加速结构把查询降到接近 O(1)。
- **内省是不动点问题**：第 N 轮读第 N-1 轮的索引，故需要反复迭代直到连续两轮一致。
- **comemo 验证是快路径**：每轮建 `Constraint`，排版后 `constraint.validate(new_introspector)` 判定「被记忆化调用读过的值有没有变」，通过即收敛、通常 1～2 轮就停。
- **`MAX_ITERS = 5` 是止损上限**：连续 5 轮未通过验证，就拼出 6 个历史 introspector（`[Empty, D1..D4, D5]`）调 `analyze`。
- **`Introspect` trait** 把每种内省抽象成 `Output`（应稳定的值）+ `introspect`（取值）+ `diagnose`（报错），`History` 用 128 位哈希比「最后两轮是否完全相同」（`0.0`≠`0`）。
- **`analyze` 才是「真未收敛」的裁判**：comemo 可能失败但 `Output` 已稳定（如归约成布尔），此时 `analyze` 产出零诊断、不发「document did not converge」警告；驱动循环结束后才把延迟错误提升为致命错误。

## 7. 下一步学习建议

本讲讲完「收敛循环」后，内省单元（第 9 单元）已完整闭环：位置表示（u9-l1）→ 可变状态（u9-l2）→ 索引与收敛（本讲）。后续建议：

1. **顺着调用链往下看排版如何建索引**：进入 `typst-layout` crate，看 `ElementIntrospectorBuilder::discover_tag` 在排版帧树时是如何被调用的，把「Tag 盖章 → 建索引」这一环补全。
2. **看一个完整目标的 Introspector 实现**：在 `typst-layout`/`typst-html` 中找到为目标实现 `Introspector` 的具体类型（paged 与 html 各一份），体会「统一接口、不同实现、部分方法返回 `None`」的设计。
3. **进入第 10 单元（数学公式）**：数学子系统的 `EquationElem` 同样依赖内省（如公式编号用计数器），届时可回看本讲，验证「计数器 → 内省 → 收敛」的闭环在数学场景下依然成立。
