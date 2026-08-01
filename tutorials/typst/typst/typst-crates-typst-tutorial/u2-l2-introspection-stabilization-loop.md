# 内省稳定化循环

## 1. 本讲目标

在 u2-l1 里，我们把 `compile_impl` 切成了七段，其中第 [6] 段「稳定化循环」只用一句话带过：*反复布局，直到 introspection 不再变化*。本讲就专门拆开这段循环，把它讲透。

读完本讲，你应当能够：

1. 解释 Typst **为什么不能「布局一次就完事」**：计数器、`query` 查询、交叉引用这些「内省」都依赖最终的页面布局结果，而页面布局又会反过来被这些内省内容影响——这是一个**鸡生蛋**问题。
2. 读懂 `compile_impl` 里那段 `loop` 的**逐行结构**：每一轮的内省器从哪来、计时标签怎么打、产物怎么入栈。
3. 说出 `comemo::Constraint` 在这里扮演的角色：它是**收敛判定的核心**，`constraint.validate(...)` 决定循环何时跳出。
4. 理解 `history`（一个 `ArrayVec`）、`MAX_ITERS = 5` 上限、以及当文档「五轮都不收敛」时 `analyze` 是怎么生成那条 *"document did not converge within five attempts"* 告警的。
5. 理解 `EmptyIntrospector` 在第一轮迭代里提供「空的内省」的意义。

> 本讲聚焦「循环本身与收敛判定的机制」。至于 `analyze` 如何遍历各条 `Introspection`、`History::converged` 如何按哈希比较、`Introspect` trait 怎么抽象「一次观察」——这些是 u2-l3 的主题，本讲只在循环碰到 `history.is_full()` 时点到为止。

## 2. 前置知识

本讲默认你已经掌握 u1-l3 与 u2-l1。这里快速复习三个关键概念：

- **`compile_impl` 七阶段**：取库 → 目标门控 → 装配样式链 → 取主源码 → 求值 → **稳定化循环** → 提升延迟错误。本讲的主角是第六阶段，它之前的五段只跑一次，把源码求值成一份 `Content` 喂给循环。
- **`Output` trait**：编译产物的抽象。`T::create(engine, content, styles)` 真正把 `Content` 布局成一份文档；`document.introspector()` 从产物里取出它的「内省器」。本讲里 `T` 通常是 `PagedDocument`。
- **`Engine` 与内省器**：`Engine` 结构体里有个字段 `introspector: Protected<Tracked<dyn Introspector>>`，布局过程中所有「问文档要信息」的调用都走它。`Introspector` 是一个 `#[comemo::track]` 的 trait，能回答 `query`、`page`、`position` 等。

再补两个本讲会用到的概念：

- **内省（introspection）**：在 Typst 里，「内省」指**在文档已经布局完成后，回头去查询它自身的信息**——比如「第 3 个标题在第几页」「这段文字之前出现了多少个图」。源码里凡是会「事后回看布局结果」的功能（`counter.display`、`query`、`locate`、`outline` 的页码……）都属于内省。
- **`comemo::Constraint`**：comemo 增量缓存框架提供的「约束」对象。你可以在拿到一个被追踪的对象（`Tracked<dyn Introspector>`）时，把它和一个新建的 `Constraint` 绑定；之后该对象上发生过的所有方法调用都会被约束「记下来」；最后用 `constraint.validate(另一个对象)` 回放这些调用，看在新对象上结果是否一致。**这个机制正是 Typst 判断「布局稳没稳」的依据**，细节见 4.3。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 本 crate 全部逻辑，主角 | `compile_impl` 里的 `loop` 块（第 133–185 行） |
| [`crates/typst-library/src/introspection/convergence.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs) | 收敛常量与未收敛分析 | `MAX_ITERS`、`ITER_NAMES`、`INSTANCES`、`analyze` |
| [`crates/typst-library/src/introspection/introspector.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/introspector.rs) | 内省器实现 | `Introspector` trait、`EmptyIntrospector` |
| [`crates/typst-library/src/foundations/target.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs) | 编译产物抽象 | `Output::introspector()` |
| [`crates/typst-library/src/engine.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | 中央上下文 | `Engine` 结构体的 `introspector` 字段、`Engine::introspect` |

## 4. 核心概念与源码讲解

### 4.1 为什么需要「反复布局」——内省的鸡生蛋问题

#### 4.1.1 概念说明

先建立一个直觉。设想这样一份文档：

```typ
#outline()   // 自动生成目录，目录里要写出每个标题的页码

= 引言
……（大段正文）……

= 方法
……（大段正文）……
```

`outline()` 生成的目录，需要知道「方法」这个标题**最终落在第几页**。但「方法」落在第几页，又取决于它前面的内容占了多少页——而它前面的内容里，**就包含目录本身**。目录里写不写页码、页码是 `3` 还是 `12`，可能改变目录的行数，进而把后面的标题推到不同的页。

于是出现循环依赖：

- 要画出目录，需要知道每个标题的页码；
- 要知道每个标题的页码，需要先完成布局；
- 而布局结果又被目录的内容所影响。

这是排版引擎里经典的「**两遍（multi-pass）**」问题。Typst 的解法是**不动点迭代**：先按某个猜测把文档布局一遍，拿到一份「内省器」（记录了谁在第几页等信息），再用这份内省器重新布局，如此反复，直到连续两轮的内省结果不再变化——也就是「收敛」了。

#### 4.1.2 核心流程

把布局抽象成一个函数 \(F\)：给定一份内省器 \(I\)（描述上一轮文档的页面信息），\(F(I)\) 产出一个新文档，并对应一个新内省器 \(\text{intro}(F(I))\)。我们要找一个**不动点** \(I^\*\)：

\[
I^\* = \text{intro}\bigl(F(I^\*)\bigr)
\]

从 \(I_0 = \text{Empty}\)（空内省器）出发做迭代：

\[
I_{n+1} = \text{intro}\bigl(F(I_n)\bigr)
\]

第 \(n\) 轮「收敛」的判定**不是**要求 \(I_{n+1}\) 与 \(I_n\) 完全相同，而是更弱、也更实际的条件——**本轮布局过程中真正问过的那些查询**，换到新内省器上答案不变即可。设第 \(n\) 轮实际发起的查询集合为 \(Q_n\)，则：

\[
\text{converged}_n \iff \forall\, q \in Q_n:\; q(I_{n+1}) = q(I_n)
\]

为什么用这个更弱的条件？因为文档里大量信息（某段文字的颜色、某个元素的尺寸）根本不影响内省，逐字节比较两份完整文档既贵又无意义；只要**被依赖的那部分信息**稳定了，下一轮布局就会得到完全一样的产物。`comemo::Constraint` 正是「记录 \(Q_n\) 并回放校验」的工具（见 4.3）。

用伪代码描述整个循环：

```text
history = []                       # 存历史文档，容量 = MAX_ITERS - 1
loop:
    introspector = history.last()?.introspector()  ??  EmptyIntrospector
    constraint   = 新建一个 Constraint
    把 introspector 用 constraint 追踪后塞进 Engine
    document     = T::create(engine, content, styles)   # 这一轮的布局

    if constraint.validate(document.introspector()):    # 收敛了？
        把本轮诊断合并进主 sink；跳出
    if history 已满:                                    # 五轮还没收敛
        组装 6 份内省器，调 analyze 生成告警；跳出
    history.push(document)                              # 否则再来一轮
```

#### 4.1.3 源码精读

循环的「地基」在求值之后立刻铺好——一个装历史文档的 `history`，以及一个首轮要用的 `empty_introspector`：[crates/typst/src/lib.rs:114-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L114-L134)。注意 `history` 的容量是 `MAX_ITERS - 1`，即 `5 - 1 = 4`：它最多只存 4 份历史文档，第 5 轮用满后会触发告警分支（见 4.4）。

```rust
let empty_introspector = EmptyIntrospector;
// ...
let mut history: ArrayVec<T, { MAX_ITERS - 1 }> = ArrayVec::new();
let mut document: T;

// Relayout until all introspections stabilize.
// If that doesn't happen within five attempts, we give up.
loop {
```

`EmptyIntrospector` 是一个「对所有查询都返回空」的内省器，定义在 [crates/typst-library/src/introspection/introspector.rs:91-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/introspector.rs#L91-L164)。它的 `query` 返回空 `EcoVec`、`page` 返回 `None`、`query_first` 返回 `None`……什么都知道不了。它的意义是：**第一轮迭代时，文档还根本不存在**，没有任何「上一轮」可参考，所以只能先假装「文档里啥也没有」地布局一遍。

```rust
impl Introspector for EmptyIntrospector {
    fn query(&self, _: &Selector) -> EcoVec<Content> { EcoVec::new() }
    fn query_first(&self, _: &Selector) -> Option<Content> { None }
    fn page(&self, _: Location) -> Option<NonZeroUsize> { None }
    // ……其余方法一律返回空 / None
}
```

> 直觉小结：循环的存在，是因为「画文档」和「读文档信息」互相依赖。`EmptyIntrospector` 给了迭代一个干净的起点，`history` 记下每一轮的产物好让下一轮有据可查。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 推理」体会「只布局一次」为什么会出错。

1. 打开 [crates/typst-library/src/introspection/introspector.rs:91-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/introspector.rs#L91-L164)，确认 `EmptyIntrospector` 的每个方法都返回「空」。
2. 假想一份只有 `#outline()` 加一个标题的最小文档。**如果 Typst 只布局一次**（即第一轮用 `EmptyIntrospector` 之后就返回），那么 `outline()` 调用 `introspector.query(<heading 选择器>)` 会得到什么？目录里的页码会显示成什么？
3. **预期结果**：第一轮里所有标题查询都返回空，目录要么拿不到任何标题、要么页码全空——这正是「只布局一次」不可行的证据，所以必须有第二轮、第三轮……直到页码稳定。

> 这一题不需要真正运行编译器，纯推理即可；现象已在源码里写死（`EmptyIntrospector` 返回空）。

#### 4.1.5 小练习与答案

**练习 1**：除了「目录页码」，再举两个**必须靠多轮迭代才能正确**的 Typst 功能，并说明它们各自依赖什么内省信息。

> **参考答案**：(1) `counter.display`——某处显示「这是第 N 个图」，N 取决于它前面出现了多少个图，这是 `query_count_before`；(2) `locate` / `context here().page()`——某段文字想知道自己在第几页，依赖 `page(location)`。两者都只能在文档布局完成后回看，因此需要迭代到稳定。

**练习 2**：如果某份文档里**完全没有**任何内省（没有计数器、没有查询、没有 `outline`），循环会跑几轮？为什么？

> **参考答案**：会跑 **1 轮**就跳出。第一轮用 `EmptyIntrospector` 布局，由于布局过程中根本没向内省器发起任何查询，`constraint` 是空的，`constraint.validate(...)` 对空约束自然返回 `true`，立即收敛。

---

### 4.2 loop 块逐行精读——取内省器、计时、布局、入栈

#### 4.2.1 概念说明

上一节讲了「为什么要循环」，本节逐行讲「循环每一轮到底干了什么」。这里只看**控制流与数据来源**，收敛判定的细节留到 4.3，`history` 满了之后的告警留到 4.4。

关键要分清三个东西：

- **本轮用的内省器**：循环每一轮开头决定，来源是「上一轮产出的文档的内省器」，没有上一轮时用 `EmptyIntrospector`。
- **本轮的 `constraint`**：每一轮**新建**一个，专门用来记录「这一轮都问了内省器什么」。
- **本轮的 `subsink`**：每一轮新建一个**临时**告警收集器。只有**收敛那一轮**的诊断才会被合并进主 `sink`，中间几轮的诊断会被丢弃——因为没收敛意味着那一轮的布局是「基于错误内省的」，它产生的告警多半是假阳性。

#### 4.2.2 核心流程

一轮迭代（从进入 `loop` 体到下一次回到 `loop` 顶部）的步骤：

```text
1. 开一个计时作用域，标签 = ITER_NAMES[history.len()]   # "iter (1)".."iter (5)"
2. introspector = history 非空 ? history.last().introspector() : EmptyIntrospector
3. constraint   = Constraint::new()
4. subsink      = Sink::new()
5. 构造 Engine，把 introspector 用 constraint 追踪后塞进去，sink 用 subsink
6. document     = T::create(&mut engine, &content, styles)   # 布局出本轮文档
7. 若 constraint.validate(document.introspector()) 成立 → 合并 subsink 到 sink，break
8. 若 history 已满                                  → 见 4.4，break
9. 否则 history.push(document)，回到第 1 步
```

注意第 1 步的计时标签用 `history.len()` 作为下标：第一轮 `history` 为空 → `ITER_NAMES[0] = "iter (1)"`，恰好对应「第 1 次迭代」。

#### 4.2.3 源码精读

循环体前半段——决定本轮内省器、新建约束与临时 sink、构造 `Engine` 并布局：[crates/typst/src/lib.rs:138-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L138-L156)。

```rust
loop {
    let _scope = TimingScope::new(ITER_NAMES[history.len()]);
    let introspector = history
        .last()
        .map(|doc| doc.introspector())
        .unwrap_or(&empty_introspector);
    let constraint = comemo::Constraint::new();

    let mut subsink = Sink::new();
    let mut engine = Engine {
        library, world,
        introspector: Protected::new(introspector.track_with(&constraint)),
        traced,
        sink: subsink.track_mut(),
        route: Route::default(),
    };

    document = T::create(&mut engine, &content, styles)?;
```

几个要点：

- **`history.last().map(|doc| doc.introspector())`**：`doc.introspector()` 来自 `Output` trait 的 [crates/typst-library/src/foundations/target.rs:29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L29)，即「从一份布局好的文档里取出它的内省器」。`.unwrap_or(&empty_introspector)` 处理「`history` 为空（第一轮）」的情形。
- **`introspector.track_with(&constraint)`**：把这份内省器与本轮的 `constraint` 绑定。布局期间对内省器发起的所有调用，都会被 `constraint` 记账。
- **`T::create` 之后的 `?`**：如果布局产生**致命**错误（`SourceResult::Err`），会直接 return，连收敛判定都跳过——致命错误是真正的「布局失败」，不是「没收敛」。

循环体后半段——收敛判定、入栈：[crates/typst/src/lib.rs:158-185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158-L185)。收敛分支把 `subsink` 合并进主 `sink` 后 `break`；否则若 `history` 没满，就 `history.push(document)` 再来一轮。

```rust
    if timed!("check stabilized", constraint.validate(document.introspector())) {
        sink.extend_from_sink(subsink);
        break;
    }

    if history.is_full() {
        // ……见 4.4：组装内省器、调 analyze、break
    }

    history.push(document);
}
```

`ITER_NAMES` 是一个固定数组，和 `MAX_ITERS` 一起定义在 [crates/typst-library/src/introspection/convergence.rs:16-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L16-L18)：

```rust
pub const MAX_ITERS: usize = 5;
pub const ITER_NAMES: &[&str] =
    &["iter (1)", "iter (2)", "iter (3)", "iter (4)", "iter (5)"];
```

#### 4.2.4 代码实践

**实践目标**：用一个表格把「每一轮的内省器来源」理清楚，体会 `history` 与迭代轮次的对应关系。

1. 阅读循环取内省器的两行（lib.rs:140-143），以及 `history.push(document)`（lib.rs:184）。
2. 假设一份文档**始终不收敛**（始终不收敛只是为了把表格填满，真实文档通常一两轮就收敛）。按下表填空，写出每一轮进入循环时 `history.len()`、计时标签、以及本轮内省器的来源：

| 进入第几轮 | `history.len()` | `ITER_NAMES` 标签 | 本轮内省器来源 |
| --- | --- | --- | --- |
| 第 1 轮 | 0 | `"iter (1)"` | `EmptyIntrospector` |
| 第 2 轮 | ? | ? | `history[0]` 的内省器（即第 1 轮产物的内省器） |
| 第 3 轮 | ? | ? | ? |
| 第 4 轮 | ? | ? | ? |
| 第 5 轮 | ? | `"iter (5)"` | `history[3]` 的内省器（即第 4 轮产物的内省器） |

3. **预期结果**：`history.len()` 依次为 0、1、2、3、4；标签依次为 `iter (1)`…`iter (5)`；本轮内省器依次来自 `Empty`、第 1 轮产物、第 2 轮产物、第 3 轮产物、第 4 轮产物。第 5 轮布局完之后 `history` 正好满了（容量 4），所以即使没收敛，第 5 轮也会走 `history.is_full()` 分支而不是再 `push`。

#### 4.2.5 小练习与答案

**练习 1**：为什么每一轮要新建一个 `constraint`，而不是整个循环共用一个？

> **参考答案**：`constraint` 记录的是「**本轮**布局过程中向**本轮**内省器发起的所有查询」。每一轮的内省器都不同（来自上一轮的产物），查询也可能不同，所以必须每轮新建、只记本轮的账。若共用一个，`validate` 会拿当前内省器去回放**所有历史轮次**的查询，语义就乱了。

**练习 2**：为什么收敛那一轮要把 `subsink` 合并进主 `sink`，而中间没收敛的轮次不合并？

> **参考答案**：没收敛的轮次是基于「过时的、错误内省」布局出来的，它产生的告警（比如「这个 label 不存在」）很可能只是因为那一轮的内省器还不知道该 label 的存在——属于假阳性。只有收敛那一轮的布局才是真正交付给用户的产物，它的告警才有意义，所以只合并那一轮。

---

### 4.3 constraint.validate——comemo 如何判定收敛

#### 4.3.1 概念说明

`constraint.validate(document.introspector())` 是整个循环的「心脏」。要理解它，得先理解 `comemo::Constraint` 的设计意图。

comemo 是 Typst 用的增量缓存框架。通常它用来自动缓存：同一个被追踪对象上、相同参数的方法调用，只算一次。但 `Constraint` 提供了另一个用法——**「把一次计算依赖了什么、假设了什么」录下来，事后换一个对象回放校验**。

具体到本场景：

1. 一开始 `introspector.track_with(&constraint)` 得到一个**和 `constraint` 绑定的内省器句柄**。
2. 布局期间（`T::create`），凡是对内省器发起的调用（`query`、`page`、`query_count_before`……）——无论是直接调用，还是经由 `Engine::introspect` 触发——都被 comemo 自动记录进 `constraint`：*「问过查询 q，当时答案是 a」*。
3. 布局产出 `document`，它自带一个**新的、真实的**内省器 `document.introspector()`。
4. `constraint.validate(新内省器)` 把记录在案的所有 `q` **在新内省器上重跑一遍**，逐个比对答案是否还是 `a`。
5. **全部一致 → 返回 `true`（收敛）**；只要有一个不一致 → 返回 `false`（本轮的假设在新文档里不成立，得再来一轮）。

这就是 4.1 里那个数学条件的实现：\( \forall q \in Q_n:\; q(I_{n+1}) = q(I_n) \)。

#### 4.3.2 核心流程

```text
constraint = Constraint::new()
绑定:  introspector' = introspector.track_with(&constraint)
布局:  T::create(engine{ introspector: introspector' }, ...)
        ↳ 期间所有对 introspector' 的调用 → constraint 记账 (q → a)
校验:  ok = constraint.validate(document.introspector())
        ↳ 用 document 的内省器重放所有 (q)，比对是否仍为 a
ok == true  ⇒ 收敛，跳出循环
ok == false ⇒ 本轮布局基于的内省与产物不符，继续迭代
```

一个微妙但重要的点：`validate` 比对的是**本轮被实际查询过的那些 q**，不是文档的全部信息。这意味着收敛是「**就本轮所依赖的信息而言**的收敛」。绝大多数文档，只依赖少数几个查询（几个页码、几个计数），所以两三轮就能收敛。

#### 4.3.3 源码精读

绑定与布局：[crates/typst/src/lib.rs:144-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L144-L156)——`Constraint::new()` 建约束，`introspector.track_with(&constraint)` 把内省器与约束绑定后塞进 `Engine`，`T::create` 执行布局。收敛判定：[crates/typst/src/lib.rs:158-161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158-L161)。

```rust
let constraint = comemo::Constraint::new();
// ...
introspector: Protected::new(introspector.track_with(&constraint)),
// ...
document = T::create(&mut engine, &content, styles)?;

if timed!("check stabilized", constraint.validate(document.introspector())) {
    sink.extend_from_sink(subsink);
    break;
}
```

那么「布局期间对内省器的调用」具体经谁发起？主要入口是 `Engine::introspect`——[crates/typst-library/src/engine.rs:109-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L109-L117)。它先解开 `Protected` 取出与 `constraint` 绑定的内省器句柄，调用具体的 `introspect` 取值，**同时把这次内省记录进 `sink`**（这个记录是给 u2-l3 的 `analyze` 用的——当 comemo 校验失败、要诊断「为什么没收敛」时，靠的就是这些记录）。

```rust
pub fn introspect<I>(&mut self, introspection: I) -> I::Output
where I: Introspect {
    let introspector = *self.introspector.access("is okay since we're recording it");
    let output = introspection.introspect(self, introspector);
    self.sink.introspection(Introspection::new(introspection));
    output
}
```

> 注意 `Protected` 这层包装：它防止代码「绕过约束绑定」直接拿到原始内省器。只有显式 `.access(...)`（语义上承认「我现在要读取它了，请计入约束」）才能取出句柄，从而保证凡是被读到的查询都进了 `constraint`。

#### 4.3.4 代码实践

**实践目标**：跟踪「内省查询 → constraint 记账 → validate 回放」这条链，确认 4.3.1 的理解。

1. 在 [crates/typst-library/src/engine.rs:109-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L109-L117) 确认 `Engine::introspect` 取出的是 `*self.introspector.access(...)`——也就是 lib.rs:150 那个与 `constraint` 绑定的句柄。
2. 用 `Grep` 在 `crates/typst-library/src/introspection/` 下搜索 `Engine::introspect` 或 `engine.introspect` 的调用点（例如 `query.rs`、`counter.rs`、`state.rs`）。挑一个看它「问」了内省器什么。
3. **预期结果**：你会看到诸如 `QueryIntrospection` 在 `introspect` 里调用 `introspector.query(&self.0)`。这正是被 `constraint` 记账的那一类调用；`validate` 之后会把同样的 `query(selector)` 在新内省器上重跑。
4. **待本地验证**（可选）：若你想直观看到「校验失败 → 再来一轮」，可在 lib.rs:158 的 `if` 之前临时加一行 `eprintln!("iter {} validate", history.len());`，然后编译一个带 `outline` 的文档运行 `typst-cli`，观察它打印几次。（这只是阅读型实践，请勿提交该改动到源码。）

#### 4.3.5 小练习与答案

**练习 1**：假设第 2 轮布局时，文档只问了内省器一个问题：「标题 A 在第几页」，当时答案 `page = 3`。第 2 轮产物的新内省器说「标题 A 在第 4 页」。`constraint.validate` 返回什么？循环会怎样？

> **参考答案**：返回 `false`（不收敛）。因为记录在案的查询 `page(A)` 当时是 3，重跑得到 4，不一致。循环不会 `break`，而是进入 `history.is_full()` 判断（这里没满），随后 `history.push(document)` 再来一轮——下一轮会基于「A 在第 4 页」重新布局。

**练习 2**：`validate` 返回 `true` 是否等价于「连续两份文档逐字节相同」？为什么？

> **参考答案**：**不等价**，前者弱得多。`validate` 只校验「本轮**实际查询过**的那些 q」在新内省器上答案不变。文档里大量与内省无关的内容（颜色、尺寸、纯文本）根本不在校验范围。这是有意为之：既省去昂贵的全文档比较，又足以保证「下一轮布局会得到相同产物」。

---

### 4.4 history 与 MAX_ITERS——上限、计时与「未收敛」告警

#### 4.4.1 概念说明

理想情况下，不动点迭代几轮就收敛。但**并非所有文档都能收敛**——如果文档的自指依赖会「震荡」（比如某内容的存在性在两个状态间反复横跳），迭代永远不会稳定。Typst 不能无限循环，于是设了硬上限 `MAX_ITERS = 5`：最多布局 5 次，超过就放弃并**发一条告警**。

这个上限由两个机制配合实现：

- `history` 的容量是 `MAX_ITERS - 1 = 4`。前 4 轮的产物依次入栈；当 `history.is_full()` 时，说明已经做了 5 轮（第 5 轮正在执行）仍未收敛，于是不再 `push`，转而进入告警分支。
- 告警分支调用 `analyze`，把循环过程中记录下来的所有 `Introspection`（由 `Engine::introspect` 存进 `subsink`）拿来逐一诊断，生成「为什么没收敛」的具体告警，并汇总成一条 *"document did not converge within five attempts"*。

#### 4.4.2 核心流程

当 `constraint.validate` 连续 5 次都返回 `false` 时：

```text
第 5 轮布局完，validate 仍 false
→ history.is_full() == true（容量 4，已装 4 份）
→ 组装 introspectors: [&Empty; 6]
    introspectors[0]     = EmptyIntrospector            # 第 1 轮用的
    introspectors[1..5]  = history[0..4] 各自的内省器    # 第 2~5 轮用的
    introspectors[5]     = document.introspector()       # 第 5 轮产物的（最终）
→ warnings = analyze(world, introspectors, subsink.introspections())
→ 合并 subsink 到主 sink；把 warnings 逐条 sink.warn(...)
→ break
```

这里的关键是 `introspectors` 数组**为什么是 6 个**（`MAX_ITERS + 1`）：它要把「5 轮迭代各自**用到**的内省器」加上「最终产物的内省器」一并交给 `analyze`，这样 `analyze` 才能对每一条 `Introspection` 重算它在每一轮的取值，画出一条「观察值序列」，据此判断是否震荡。

#### 4.4.3 源码精读

告警分支：[crates/typst/src/lib.rs:163-182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L163-L182)。

```rust
if history.is_full() {
    let mut introspectors =
        [&empty_introspector as &dyn Introspector; MAX_ITERS + 1];
    for i in 1..MAX_ITERS {
        introspectors[i] = history[i - 1].introspector();
    }
    introspectors[MAX_ITERS] = document.introspector();

    let warnings = typst_library::introspection::analyze(
        world,
        introspectors,
        subsink.introspections(),
    );

    sink.extend_from_sink(subsink);
    for warning in warnings {
        sink.warn(warning);
    }
    break;
}
```

逐项对应 4.4.2 的流程：`introspectors[0]` 初始化为 `empty_introspector`（第 1 轮用的）；`for i in 1..MAX_ITERS`（即 `i = 1,2,3,4`）把 `history[0..4]` 的内省器填进 `introspectors[1..5]`（第 2~5 轮用的）；`introspectors[5]` 是最终产物。常量 `INSTANCES = MAX_ITERS + 1 = 6` 定义在 [crates/typst-library/src/introspection/convergence.rs:20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L20)。

`analyze` 的入口与「汇总告警」逻辑：[crates/typst-library/src/introspection/convergence.rs:25-70](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L25-L70)。它遍历本轮记录的每条 `Introspection`，让各自 `diagnose` 决定要不要报警；最后若有任何具体告警，就插入一条汇总：

```rust
let mut diags = sink.warnings();
if !diags.is_empty() {
    let summary = warning!(
        Span::detached(),
        "document did not converge within five attempts";
        hint: "see {} additional warning{} for more details", diags.len(), /* ... */;
        hint: "see https://typst.app/help/convergence for help";
    );
    diags.insert(0, summary);
}
```

> 一个**重要细节**（源码注释 convergence.rs:37-55 解释）：`analyze` 只有在「至少有一条 `Introspection` 诊断出未收敛」时才发汇总告警。如果 comemo 的 `validate` 失败了（所以才会走到 `analyze`），但**所有** `Introspection` 用 `History::converged` 判断却都说「其实收敛了」，那就**不发告警**。这种情况对应「这条内省对数据做了过滤/归约，comemo 看到的原始数据在变，但用户关心的归约值其实已经稳定」——文档事实上收敛了，不该误报。这块判定逻辑（`History::converged` 按 128 位哈希比较第 `MAX_ITERS-1` 与第 `MAX_ITERS` 个值）是 u2-l3 的核心，本讲只点到这里。

#### 4.4.4 代码实践

**实践目标**：理解 `MAX_ITERS` 上限与 `analyze` 的触发时机，能预测「五轮不收敛」时会发生什么。

1. 阅读告警分支（lib.rs:163-182），数一数 `introspectors` 数组最终有几个非空槽被填入「真实内省器」。
2. 阅读汇总告警的生成条件（convergence.rs:56-69），回答：如果走到 `analyze` 但 `diags` 为空，用户会看到几条收敛相关告警？
3. **预期结果**：`introspectors` 共 6 个槽：`[0]` 是 `EmptyIntrospector`，`[1..5]` 是 `history[0..4]` 的内省器，`[5]` 是最终产物内省器——即 5 个「真实」内省器加 1 个空内省器。若 `diags` 为空，则**一条收敛告警都不会发**（连汇总也不发），文档照常返回。
4. **待本地验证**（可选）：尝试写一个会震荡的 Typst 文档（例如让某元素的存在性依赖于自身页码的奇偶），用 `typst-cli` 编译，观察是否真的出现 *"document did not converge within five attempts"* 以及附带的细节告警。震荡文档较难构造，若一时写不出，可先阅读 [convergence.rs:37-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L37-L55) 的注释理解触发条件，不勉强。

#### 4.4.5 小练习与答案

**练习 1**：`history` 的容量为什么是 `MAX_ITERS - 1` 而不是 `MAX_ITERS`？

> **参考答案**：循环最多跑 5 轮。前 4 轮没收敛时，产物依次 `push` 进 `history`，填满容量 4；第 5 轮进入循环时 `history.len() == 4`（已满），布局完若仍不收敛，走 `history.is_full()` 分支而**不再 push**。所以 `history` 只需容纳「第 1~4 轮的产物」共 4 份，第 5 轮的产物以 `document` 局部变量的形式直接交给 `analyze`（放进 `introspectors[5]`），无需入栈。

**练习 2**：用户看到的「未收敛」告警，信息是从哪里来的？为什么 `analyze` 需要那 6 份内省器？

> **参考答案**：来自本轮 `subsink.introspections()`——即第 5 轮布局过程中、由 `Engine::introspect` 记录下来的每一条 `Introspection`（如「某 query 的结果」「某 counter 的值」）。`analyze` 拿这 6 份内省器（对应 5 轮迭代 + 最终产物），对每条 `Introspection` 在 6 个内省器上分别重算取值，得到一条「观察值序列」，从而能告诉用户「这个值在 5 轮里是 1→2→1→2→2 这样震荡的」之类可读的诊断。

---

## 5. 综合实践

把本讲四个模块串起来，做一个完整的「目录页码」推演。这是本讲规格里指定的实践：**用一个具体例子（「目录页码依赖正文页数」）推演前 3 轮迭代各自的内省器状态，并说明第几轮会触发 `constraint.validate` 返回 `true` 而跳出循环。**

**场景文档**（示意，不必实际运行）：

```typ
#outline()   // 目录：列出所有标题及其页码

= 引言
（正文，恰好占满第 1 页）

= 方法
（正文，从第 2 页开始）
```

设布局规则稳定后，「方法」标题应在第 2 页，目录本身不额外占行（页码宽度不影响换行）。

**实践目标**：填完下表并回答收敛轮次。

**操作步骤**：

1. 对照 4.2 的「每轮内省器来源」与 4.3 的「validate 语义」，逐轮推演。
2. 在「本轮向内省器问了什么」「当时答案」「validate 回放结果」三列里推理填写。

| 轮次 | 本轮内省器来源 | `outline` 向内省器问的问题 | 当时得到的答案 | 本轮布局后「方法」实际在第几页 | `validate(新内省器)` 回放结果 |
| --- | --- | --- | --- | --- | --- |
| 第 1 轮 | `EmptyIntrospector` | 「方法」标题在第几页？ | `None`（空内省器什么都答不了） | 第 2 页（正文布局本身不依赖目录页码） | 重放：问「方法」页码 → 新内省器答「第 2 页」≠ `None` → **不一致** |
| 第 2 轮 | 第 1 轮产物的内省器（知道「方法」在第 2 页） | 「方法」标题在第几页？ | 第 2 页 | 第 2 页 | 重放：问「方法」页码 → 新内省器答「第 2 页」== 第 2 页 → **一致** |
| 第 3 轮 | （不会执行） | — | — | — | — |

**需要观察的现象 / 预期结果**：

- **第 1 轮**：目录拿不到任何页码（或显示为空），但正文布局与目录页码无关，所以「方法」仍落在第 2 页。`validate` 发现「这一轮假设没有页码信息，但新文档明明有」→ 不一致 → 不收敛 → `history.push(doc1)`。
- **第 2 轮**：内省器已知道「方法」在第 2 页，目录正确显示「方法 …… 2」。显示「2」不改变换行，「方法」依旧在第 2 页。`validate` 回放唯一的查询得到一致答案 → **收敛，`break`**。
- **结论**：**第 2 轮**触发 `constraint.validate(...) == true`，循环在第 2 次迭代后跳出。第 3 轮根本不会执行。

> 进阶思考（可选）：若把文档改成「目录内容很多，写不写页码会改变正文换页」，收敛可能延后到第 3 轮；若依赖关系会震荡，则会一路跑到第 5 轮触发 `analyze` 告警。能预判「几轮收敛」是理解本讲的标志。

## 6. 本讲小结

- Typst 之所以要**反复布局**，是因为计数器、`query`、`outline` 页码等「内省」依赖最终布局结果，而布局又反过来被这些内省内容影响——这是不动点迭代问题。
- `compile_impl` 的 `loop` 每轮：从 `history.last()` 取上一轮产物的内省器（首轮用 `EmptyIntrospector`），新建一个 `Constraint` 与临时 `subsink`，调 `T::create` 布局，再用 `constraint.validate(document.introspector())` 判定收敛。
- **收敛判定的核心是 `comemo::Constraint`**：它记录本轮布局问过内省器的所有查询及答案，`validate` 把这些查询在新内省器上重跑，全一致才算收敛——这是「就所依赖信息而言」的弱收敛，既足够保证下一轮产物不变，又避免了昂贵的全文档比较。
- 循环至多 `MAX_ITERS = 5` 轮；`history` 容量为 `MAX_ITERS - 1 = 4`，第 5 轮用满后走告警分支。`ITER_NAMES` 给每轮打上 `iter (1)`…`iter (5)` 的计时标签。
- 五轮不收敛时，`analyze` 用 6 份内省器（`Empty` + 4 份历史 + 最终产物）与本轮记录的 `Introspection` 序列生成诊断；但若所有 `Introspection` 其实已收敛（comemo 误判），则**不发**任何收敛告警。
- 只有**收敛那一轮**的 `subsink` 才合并进主 `sink`，中间轮的诊断被丢弃，避免「基于过时内省」的假阳性告警。

## 7. 下一步学习建议

本讲把「循环怎么转、何时停」讲透了，但有意把 `analyze` 内部留了一半。下一讲 **u2-l3「内省记录与非收敛检测」** 正好接上：

- 精读 `Introspect` trait（`Output` / `introspect` / `diagnose` 三件套）与类型擦除的 `Introspection`；
- 拆开 `analyze` 如何遍历 `Introspection`、用 `History::compute` 重算每轮取值；
- 弄懂 `History::converged` 为什么按 128 位哈希比较、`History::hint` 如何生成「run 1 / run 2 / … / final」的可读诊断。

读完 u2-l3，再结合 u2-l4（`Engine` / `Sink` / `Route` / `Traced`），你就能把「循环 + 上下文 + 诊断」整张拼图补全。建议在进入 u2-l3 前，回头把本讲 4.4.3 里引用的 [convergence.rs:37-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L37-L55) 注释再读一遍——它预告了 u2-l3 的核心论点。
