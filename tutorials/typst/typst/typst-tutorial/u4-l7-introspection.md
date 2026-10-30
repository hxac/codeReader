# 内省机制：counter、引用与两遍编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释为什么交叉引用、目录、页码计数器会迫使编译器**多轮排版**，以及 Typst 如何用「迭代直到收敛」解决这个问题。
2. 说出 `Location`、`Locatable`、`Tag`、`Introspector` 四个概念各自的含义与它们之间的数据流关系。
3. 描述 `PagedIntrospector` 如何从已经排完版的 `Frame` 中收集元素与位置，并建成可供查询的索引。
4. 描述 `query` / `counter` 在数据层如何变成一次「对上一轮文档的读取」（`Introspect` trait），以及这种读取如何被编译器记录下来用于收敛诊断。
5. 解释两遍（乃至五遍）编译的收敛判定条件、不收敛时警告如何生成，以及多轮迭代对性能的影响和 comemo 缓存在哪些位置起作用。

本讲是排版管线单元（第四单元）的收官：前面几讲我们走完了 realize → 页面 → flow → 行内 → 网格，本讲回答的问题是——**当排版结果本身又成为排版的输入时，会发生什么？**

## 2. 前置知识

### 2.1 鸡生蛋问题：正向引用

考虑一个目录页：它想列出每个章节标题所在的页码。但页码只有在**整个文档排完版**之后才知道；而目录页本身的长度又会影响后面内容的分页。这就是排版的「鸡生蛋问题」（forward reference）：

- LaTeX 的做法是编译多遍，把页码写进 `.aux` 辅助文件，下一遍读取；
- Typst 的做法是在**同一次编译的内存里**反复排版，直到文档不再变化（不动点迭代）。

这种「排版结果反过来查询排版结果」的机制，Typst 官方称为**内省（introspection）**。

### 2.2 你需要先掌握的旧知识

本讲建立在前面讲义之上，需要你记得：

- **u3-l7（样式系统）**：`context` 表达式在求值期被挂起为 `ContextElem`，等排版期有了位置与样式链再回放——内省函数全部要求在 `context` 中调用，原因本讲会揭晓。
- **u4-l2（realize 阶段）**：realize 输出「元素 + 生效样式链」的扁平列表 `Vec<Pair>`，其中有一种特殊元素 `TagElem`（当时被称为「Tag/TagElem」新术语），本讲会完整讲清它。
- **u4-l3（页面与文档）**：`layout_document` 产出 `PagedDocument`，内含每页一个 `Frame`；`PagedDocument::new` 内部「顺便构建了 introspector」这句话当时一笔带过，本讲正是它的展开。
- **u4-l1（编译主流程）**：编译循环中**求值只执行一次，循环的是排版**；comemo 通过 `Constraint` 追踪本轮流式读取了哪些数据。本讲会看到 `constraint.validate` 在这里的具体用法。

### 2.3 术语表

| 术语 | 含义 |
|---|---|
| 内省 introspection | 在排版过程中查询「已完成文档」的信息（位置、计数、元素集合） |
| `Location` | 一个 128 位哈希，唯一标识文档中一个元素**实例** |
| Locatable 可定位元素 | 被 `#[elem(Locatable)]` 标记、自动获得 `Location` 的元素类型 |
| `Tag` | 打进 `Frame` 里的开始/结束标记，是 `Location` 的物理载体 |
| `Introspector` | 已完成文档的「查询数据库」，从 `Frame` 中收集所有 `Tag` 而来 |
| 收敛 convergence | 连续两轮排版产出的内省观测值完全一致 |
| `MAX_ITERS` | 排版迭代上限，当前为 5 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs) | 总装 crate：`compile_impl` 中的多轮排版主循环 |
| [crates/typst-library/src/introspection/convergence.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs) | `MAX_ITERS`、`Introspect` trait、`History`、不收敛诊断 `analyze` |
| [crates/typst-library/src/introspection/introspector.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs) | `Introspector` trait 与目标无关的查询引擎 `ElementIntrospector` |
| [crates/typst-layout/src/introspect.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs) | `PagedIntrospector`：遍历页面 `Frame` 收集元素与页面位置 |
| [crates/typst-library/src/introspection/location.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs) | `Location` 类型、`Locatable` 元素清单文档、各类位置内省 |
| [crates/typst-library/src/introspection/tag.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs) | `Tag`（Start/End）与 `TagElem` |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs) | `prepare`：给可定位元素分配 `Location` 并生成一对 `Tag` |
| [crates/typst-library/src/introspection/query.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs) | `query` 函数与 `QueryIntrospection` 等 |
| [crates/typst-library/src/introspection/counter.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs) | `Counter`、计数更新元素、计数回放与页面计数器 |
| [crates/typst-library/src/introspection/locator.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs) | `Locator`：分层哈希地分配稳定 `Location` |
| [crates/typst-layout/src/document.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs) | `PagedDocument` / `Page` 结构，`Output` trait 的 introspector 接口 |
| [crates/typst-cli/src/eval.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/eval.rs) | `typst eval` 子命令：编译文档后对最终 introspector 求值表达式 |

> 说明：`crates/typst-library/src/lib.rs:19` 以 `pub mod introspection;` 挂载内省模块，[crates/typst-library/src/introspection/mod.rs:35-45](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/mod.rs#L35-L45) 把 `Location`、`Counter`、`State`、`metadata`、`here`、`query`、`locate` 注册进全局作用域；[crates/typst-layout/src/lib.rs:L8](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lib.rs#L8) 声明 `mod introspect;` 并在 [L22](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lib.rs#L22) 导出 `PagedIntrospector`。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

1. 为什么需要多轮编译：内省与主循环
2. `Location` 与 Locatable 元素体系（含 `Tag` 的诞生）
3. Introspector 收集：从 `Frame` 到查询索引
4. `query` / `counter` 的内省语义
5. 迭代收敛判定、不收敛诊断与性能

---

### 4.1 为什么需要多轮编译：内省与主循环

#### 4.1.1 概念说明

内省要解决的核心矛盾是：**排版需要的信息（页码、编号、总数）在排版完成前不存在**。

Typst 的解法可以概括为一句话：

> 先假设一份「上一轮的文档」，排版时所有内省查询都读它；排完后再检查「本轮读到的答案」放在新文档里是否依然成立；不成立就用新文档当「上一轮」，再来一遍。

这就是**不动点迭代**：只要文档不再变化（不动点），查询结果就自洽了。绝大多数文档 2~3 轮就收敛；理论上限 5 轮，超过则放弃并发出「不收敛」警告（注意：是警告不是错误，编译仍然产出最后一轮的文档）。

#### 4.1.2 核心流程

`compile_impl` 的主干（伪代码）：

```text
content = eval(main_source)            # 求值只做一次，在循环之外
history = 容量 4 的旧文档队列            # MAX_ITERS - 1
loop:
    introspector = history 最后一份文档的 introspector
                 （第一轮没有历史 → EmptyIntrospector，所有查询返回空）
    constraint = comemo::Constraint::new()
    document  = 排版(content, introspector 以 constraint 追踪)
                # 排版期间每一次对 introspector 的读取都被 constraint 记账

    if constraint.validate(document.introspector()):
        break                          # 本轮读到的数据在新文档中仍成立 → 收敛

    if history 已满:
        analyze(所有记录过的内省请求)     # 生成「不收敛」警告
        break

    history.push(document)             # 用新文档当下一轮的「上一轮」
```

两个关键点：

- **第一轮是「盲排」**：没有任何历史文档，introspector 是空的 `EmptyIntrospector`，所以第一轮里 `query` 返回空数组、`counter` 从初值开始、`loc.position()` 返回 `none`。第一轮的意义就是把 `Tag` 铺进 `Frame`，造出第一份可查询的文档。
- **收敛判定在编译器手里，不在用户手里**：用户写 `#context query(heading)` 时并不指定迭代次数，是否再排一轮由 `constraint.validate` 决定。

#### 4.1.3 源码精读

先看总装 crate 的主循环。求值发生在循环**之外**——这印证了 u4-l1 的结论「求值只执行一次，循环的是排版」：

[crates/typst/src/lib.rs:L123-L132](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L123-L132) —— 获取主源文件并**只求值一次**，得到 `content`（元素树）。此后的循环只围绕 `content` 反复排版。

[crates/typst/src/lib.rs:L134-L145](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L134-L145) —— `history` 是容量为 `MAX_ITERS - 1`（即 4）的 `ArrayVec`；每轮循环用 `TimingScope` 打上 `iter (1)`、`iter (2)`……的时间标记（这就是实践里数轮数的依据）；`introspector` 取自 `history.last()`，没有历史时用 `empty_introspector`；`constraint` 是本轮的「记账本」。

[crates/typst/src/lib.rs:L146-L157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L146-L157) —— 引擎的 introspector 通过 `introspector.track_with(&constraint)` 交给排版，意味着排版期间对 introspector 的每次读取都被 comemo 记录在 `constraint` 里；随后 `T::create` 执行整轮排版（对 paged 目标就是 `layout_document`）。

[crates/typst/src/lib.rs:L159-L186](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L159-L186) —— 收敛判定三连：

- L159：`constraint.validate(document.introspector())` —— 「本轮从旧 introspector 读到的所有数据，在新文档的 introspector 里还成立吗？」成立则收敛，`break`；
- L164：`history.is_full()` —— 已经排了 5 轮还没稳定，调用 `introspection::analyze` 生成警告后放弃；
- L185：否则把本轮文档压入历史，进入下一轮。

再看常量定义：

[crates/typst-library/src/introspection/convergence.rs:L16-L18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L16-L18) —— `MAX_ITERS = 5` 与 `ITER_NAMES`（五轮的计时名称）。迭代上限选 5 是经验值：正常文档 2~3 轮收敛，病态文档（查询影响自身）多排也无济于事，及时止损并警告。

而 `PagedDocument` 是在哪里挂上 introspector 的？在 u4-l3 我们见过 `PagedDocument::new`，当时没展开的这句话现在可以读懂了：

[crates/typst-layout/src/document.rs:L29-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L29-L41) —— `PagedDocument::new` 内部调用 `PagedIntrospector::new(&pages)`：**文档一旦排完，introspector 立即从页面帧里构建出来**，成为下一轮排版的「上一轮文档」。`Output` trait 的 `introspector()` 方法（[document.rs:L78-L82](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L78-L82)）则让总装循环可以用统一接口拿到它——无论目标是 paged 还是 html。

#### 4.1.4 代码实践

**实践目标**：用 `--timings` 亲眼数出一篇带目录/交叉引用的文档排了几轮。

**操作步骤**：

1. 写一个 `toc.typ`：

   ```typ
   #set page(numbering: "1")
   #outline()

   = Introduction <intro>
   #lorem(80)

   = Discussion
   See @intro for the introduction.
   #lorem(80)
   ```

2. 用仓库里的 CLI 编译并导出计时（`--timings` 的定义见 [crates/typst-cli/src/args.rs:L465-L471](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L465-L471)）：

   ```sh
   cargo run -p typst-cli -- compile --timings timings.json toc.typ
   ```

3. 打开 `timings.json`（Chrome Tracing 格式），搜索字符串 `iter (`。每个布局轮次对应一个名为 `iter (1)`、`iter (2)`……的计时区间（名字来自 `ITER_NAMES`）。

4. 删掉 `#outline()` 与 `@intro`（引用），再编译一次，重新数 `iter (` 的个数。

**需要观察的现象**：带目录与引用的文档应出现至少 2 个 `iter (N)` 区间；删掉内省依赖后应只剩 1 个（一轮即收敛）。

**预期结果**：`timings.json` 中 `iter (N)` 的最大编号 = 实际布局轮数。具体轮数取决于分页是否被目录长度扰动，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么第一轮排版时 `#context query(heading)` 返回空数组，编译却不报错？

**答案**：第一轮的 introspector 是 `EmptyIntrospector`（[introspector.rs:L91-L164](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L91-L164)），`query` 对它只是返回空集合；这些「空答案」与后续轮次的「真答案」不一致，恰好触发下一轮迭代。查询结果为空不是错误，只有「五轮后仍不稳定」才会警告。

**练习 2**：求值（`eval`）会随迭代重复执行吗？这带来什么好处？

**答案**：不会。`eval` 在循环之前只执行一次（[typst/src/lib.rs:L124-L132](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L124-L132)）。好处：(1) 用户脚本中的副作用（如 `sys` 输出）不会重复发生；(2) 最贵的 `Content` 树只建一次，循环里复用同一棵树做排版，配合 comemo 记忆化，未受扰动的大段内容直接命中缓存。

**练习 3**：如果文档五轮后仍不收敛，编译器会输出文档吗？

**答案**：会。`history.is_full()` 分支只是收集 `analyze` 产生的警告然后 `break`（[typst/src/lib.rs:L164-L183](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L164-L183)），`document` 仍是最后一轮的完整产物，函数最后 `Ok(document)` 返回。不收敛是警告，不是错误。

---

### 4.2 `Location` 与 Locatable 元素体系：元素的身份证

#### 4.2.1 概念说明

要让「上一轮文档」可查询，必须先能给文档里的每个元素实例发一张**身份证**。这张身份证就是 `Location`：

- 它是一个 `u128` 哈希（不是自增序号，也不是源码位置）；
- 它标识的是「文档中这个位置上的这个元素实例」，同一个 `#for` 循环生成的 5 个 figure 各有不同 `Location`；
- 与 `Span`（u2-l1/u2-l4）区分开：`Span` 回答「这段代码来自源文件哪里」，`Location` 回答「这个元素实例排在文档哪里」。

**谁有资格获得身份证？** 由元素声明处的 `#[elem(Locatable, ...)]` 能力标记决定，这类元素称为 **Locatable（可定位）元素**。设计原则是：**语义元素才可定位**——标题、图、脚注这类会被引用/计数/查询的元素；而 `text`、`box` 这类纯排版元素默认不可定位。此外，**任何被贴了 `<标签>` 的元素也会临时获得 `Location`**（否则 `<标签>` 引用无从谈起）。

#### 4.2.2 核心流程

`Location` 的产生与消费链路：

```text
#[elem(Locatable)] 声明
    │  (typst-macros 生成的 IntrospectionCapabilities)
    ▼
realize 的 prepare 阶段：elem.is_locatable()？
    │ 是（或有标签/已有 location）
    ▼
Locator::next_location  → 分配一个分层哈希的 u128
    ▼
给元素打一对 Tag：Tag::Start(元素快照) + Tag::End(location, key)
    ▼
TagElem 混入排版流 → 布局器把它写进 Frame（FrameItem::Tag）
    ▼
（下一节）Introspector 从 Frame 收集这些 Tag
```

`Locator` 的分层哈希策略（为什么要哈希而不是计数器）在 4.5.3 的性能视角里展开，这里只需要知道：**`Location` 的设计目标之一是跨轮次、跨编辑保持稳定**，这样第二轮里的 heading 和第一轮里的同一个 heading 拿到同一个 `Location`，查询才能对上号。

#### 4.2.3 源码精读

`Location` 本体非常朴素——就是 `u128` 的薄包装：

[crates/typst-library/src/introspection/location.rs:L55-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L55-L78) —— `Location(u128)`：`new` 从哈希构造、`hash` 取回原值、`variant(n)` 从一个已有 location 派生「变体」（文献目录用它给每条参考文献造出独立可链接的位置）。

[crates/typst-library/src/introspection/location.rs:L151-L167](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L151-L167) —— `Debug` 输出默认截断为低 16 位以便阅读（`Location(12345)`），`Repr` 在脚本里显示为 `location(..)`——脚本层拿不到哈希本体，只能把它当作不透明句柄传递。

哪些元素是 Locatable？**权威清单就维护在 `Location` 的文档注释里**：

[crates/typst-library/src/introspection/location.rs:L26-L54](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L26-L54) —— 逐类别列出的可定位元素：Model 类最多（`asset`、`bibliography`、`cite`、`divider`、`document`、`emph`、`enum`、`figure`、`figure.caption`、`footnote`、`footnote.entry`、`heading`、`link`、`list`、`outline`、`outline.entry`、`par`、`quote`、`ref`、`strong`、`table`、`terms`、`title`）；Text 类有 `raw` 与装饰元素 `underline`/`overline`/`strike`/`highlight`；Introspection 类的 `metadata`（被查询就是它的主业）；其余类别的例外是 `math.equation`、`image`、`pdf.attach`。文档同时说明：非可定位元素贴了标签后同样能被观察到。

值得注意的一个**新近变化**：`divider`（主题分隔线）现在是可定位的——

[crates/typst-library/src/model/divider.rs:L40-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/divider.rs#L40-L41) —— `#[elem(since = "0.15.0", Locatable, ShowSet)]`。这正是本仓库最近一次提交之一「Make `divider` locatable (#9023)」的结果（见 `git log` 中 `26d92af60`），对应上面清单里 `divider` 的出现：现在 `#query(divider)` 可以统计全文出现了多少条分隔线。

`Locatable` 标记如何变成运行时判断？宏展开时会生成能力位：

[crates/typst-macros/src/elem.rs:L702-L724](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L702-L724) —— `create_introspection_capabilities` 把 `Locatable`/`Unqueriable`/`Tagged` 三个内省能力编译成 `IntrospectionCapabilities { locatable, unqueriable, tagged }` 位标志，供 `elem.is_locatable()` 之类的检查使用（u5-l5 会从宏的角度再讲一遍）。

真正发放身份证的现场在 realize 的 `prepare`（u4-l2 讲过 prepare 负责「一次性完成合成与物化」，当时略过了打标签）：

[crates/typst-realize/src/lib.rs:L540-L556](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L540-L556) —— 三步：(1) `key = hash128(&elem)` 算出元素的内容哈希；(2) 组装 `TagFlags`，其中 `introspectable = elem.is_locatable() || elem.label().is_some() || elem.location().is_some()`——**可定位、带标签、或来自查询（已带 location）三种情况之一**；(3) 若还没有 `Location` 且确有需要，向 `Locator` 申请一个并 `set_location` 写回元素。

[crates/typst-realize/src/lib.rs:L575-L582](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L575-L582) —— 在合成与物化**之后**（这样标签里的元素快照包含合成字段），生成一对标签 `(Tag::Start(elem.clone(), flags), Tag::End(loc, key, flags))`。注意 Start 里存的是**整个元素的克隆**——这就是为什么 `query` 能返回带完整字段的元素。

标签的载体定义：

[crates/typst-library/src/introspection/tag.rs:L10-L24](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L10-L24) —— `Tag` 只有两种形态：`Start(Content, TagFlags)`（元素从这里开始，附完整快照）与 `End(Location, u128, TagFlags)`（该元素到此结束；key 哈希放在 End 里只是为了让两个变体内存大小平衡——注释原话如此）。

[crates/typst-library/src/introspection/tag.rs:L46-L61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L46-L61) —— `TagFlags { introspectable, tagged }`：前者决定这个标签会不会进入 Introspector；`tagged` 是另一类需要打标但不需要被查询的元素。

[crates/typst-library/src/introspection/tag.rs:L63-L83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L63-L83) —— `TagElem`：把 `Tag` 包装成一种**不可由用户构造**（`Construct` 实现直接报错 "cannot be constructed manually"）的内部元素，文档注释写明「所有布局器都会处理它；它承载的元素在**下一次编译迭代**中变得可内省」——这句话正是本讲主题的官方表述。

最后，`query` 对「不可定位元素」的拒绝发生在参数类型转换处：

[crates/typst-library/src/foundations/selector.rs:L421-L433](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/selector.rs#L421-L433) —— `LocatableSelector::from_value` 校验选择器里的每个元素函数：`!elem.is_locatable() || elem.is_unqueriable()` 时报错「`{} is not locatable`」。所以 `query(text)` 会在**求值期**（而不是排版期）直接报类型错误。

#### 4.2.4 代码实践

**实践目标**：验证 `divider` 现在可被查询，并对比查询一个不可定位元素时的报错。

**操作步骤**：

1. 写 `dividers.typ`：

   ```typ
   First part
   #divider()
   Second part
   #divider()
   Third part
   ```

2. 用 `typst eval` 在文档上下文里查询（`eval` 子命令的实现在 [crates/typst-cli/src/eval.rs:L32-L129](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/eval.rs#L32-L129)：先编译 `--in` 的文档，再把表达式对着最终 introspector 求值）：

   ```sh
   cargo run -p typst-cli -- eval 'query(divider).len()' --in dividers.typ
   ```

3. 再故意查询一个不可定位元素：

   ```sh
   cargo run -p typst-cli -- eval 'query(text)' --in dividers.typ
   ```

**需要观察的现象**：第 2 步应输出 `2`；第 3 步应报错，错误消息形如 `text is not locatable`。

**预期结果**：`query(divider).len()` 返回 `2`（两条分隔线都被收集）；`query(text)` 报 `text is not locatable`。此用法源自 [query.rs:L103-L131](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L103-L131) 的官方文档示例（该示例用 `query(<note>)` 查询 `metadata`）。具体输出格式**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Span` 和 `Location` 都能「定位」，它们定位的对象有什么不同？

**答案**：`Span` 定位**源码**——一个 8 字节编号，高 16 位是 `FileId`，用于把错误/提示指回源文件字节区间（u2-l4）；`Location` 定位**文档中的元素实例**——一个 u128 内容哈希，用于跨轮次识别「同一个元素」。一个 `Span` 可以对应多个 `Location`（如 `#for` 循环五次生成五个 figure，同源码处），反之一个元素实例只有一个 `Location`。

**练习 2**：为什么 `Tag::Start` 里要克隆整个元素，而不是只存 `Location`？

**答案**：因为 `query` 的语义是返回**元素本身**（含全部字段，用户还要读 `chapter.body`、`figure.caption` 等字段）。Introspector 若只存 `Location`，查询结果就只是个句柄，用户拿不到内容。克隆发生在 realize 的 `prepare`（[realize/lib.rs:L580-L582](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L580-L582)），且在合成与物化之后进行，保证快照里有合成字段。`Content` 是 `Arc` 化的廉价克隆（u3-l4），所以代价可控。

**练习 3**：`#metadata("note") <mynote>` 里的 `metadata` 为什么天然是 Locatable？

**答案**：`metadata` 的存在意义就是「被查询」——它是用户往文档里塞自定义标记、再在别处用 `query(<mynote>)` 取回的主要手段（见 [location.rs:L44-L45](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L44-L45) 的说明「being queried for is its primary purpose」）。它不可见、无排版副作用，可定位是它的设计目的。

---

### 4.3 Introspector 收集：从 `Frame` 到查询索引

#### 4.3.1 概念说明

`Introspector` 是「已完成文档」的查询数据库。它的输入是排完版的页面帧，输出是一张**按文档顺序排列**的元素表，外加几组加速索引。

typst-library 提供了目标无关的内核 `ElementIntrospector<P>`（`P` 是位置类型），typst-layout 提供 paged 目标的实现 `PagedIntrospector`（`P = PagedPosition`，即「第几页 + 页内坐标」）。这样 html 等其他目标可以复用同一套查询引擎，只是位置语义不同。

`Introspector` trait 本身带 `#[comemo::track]`——这一点至关重要：主循环正是通过 `track_with(&constraint)` 追踪「本轮读了哪些条目」，才能在排完后做收敛判定（见 4.5）。

#### 4.3.2 核心流程

收集流水线：

```text
PagedIntrospector::new(pages)
    │ 逐页：记录 page numbering / supplement
    ▼
discover_frame(frame, transform)
    │ 递归遍历帧内条目，坐标系随 Group 变换逐层复合
    ├─ FrameItem::Tag(t)      → builder.discover_tag(t, 页内绝对坐标)
    ├─ FrameItem::Group(g)    → 递归；若 g.parent 存在（测量场景）
    │                            则把这段发现「登记为 parent 的插入件」
    ├─ FrameItem::Link(dest)  → 记入 frame_link_targets（导出时判断死链）
    └─ Text/Shape/Image       → 忽略（内省只关心 Tag）
    ▼
ElementIntrospectorBuilder（sink 里攒 Start/End 事件流）
    ▼
finalize / visit：展平成有序 elems 数组，同时建三张索引：
    locations: Location → Range（元素及其全部后代在数组中的区间）
    labels:    Label → [下标]
    keys:      key 哈希 → [Location]（测量模式的位置配对用）
```

查询时（`query(selector)`）按选择器形态选择策略，结果**始终按文档顺序**返回，并有进程内查询缓存 `QueryCache`。

#### 4.3.3 源码精读

先看 paged 侧的入口：

[crates/typst-layout/src/introspect.rs:L37-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L37-L58) —— `PagedIntrospector::new`（带 `time` 计时标注）：逐页收集 `numbering`/`supplement`，对每页调用 `discover_frame`，闭包 `to_pos` 把帧内坐标包装成 `PagedPosition { page: 页号, point }`。注意页号从 1 开始（`NonZeroUsize::new(1 + i)`）。

[crates/typst-layout/src/introspect.rs:L176-L207](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L176-L207) —— `discover_frame` 是一次**带坐标变换的前序遍历**：

- L183-L185：遇到 `FrameItem::Tag` 就把标签连同「经过变换后的绝对坐标」交给 builder；
- L186-L198：遇到 `Group` 先把 `Transform::translate(pos)` 与群自身变换 `pre_concat` 进累计变换 `ts`，再递归。**关键分支**在 L191-L195：`group.parent` 存在时（这是被「测量」后放置的内容，见 locator.rs 的 Dealing with Measurement 一节），用 `start_insertion`/`end_insertion(parent.location)` 把这段发现登记为父元素的「插入件」，排序时归到父元素名下；
- L199-L203：`Link` 目标是 `Destination::Location` 时记入 `frame_link_targets`（PDF/SVG 导出用它判断链接目标是否存在）；
- L204：文本/形状/图像对内省不可见。

再看目标无关的查询内核。trait 定义：

[crates/typst-library/src/introspection/introspector.rs:L28-L89](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L28-L89) —— `#[comemo::track] pub trait Introspector`：`query`/`query_first`/`query_unique`/`query_label`/`query_labelled`/`query_count_before`（计数器专用优化）/`label_count`/`locator`（测量配对）/`pages`/`page`/`position`/`page_numbering`/`page_supplement` 等。文档注释明确：由**目标特定**的 introspector 实现，html 目标对 `page` 类请求可以返回 `None`。

数据结构与索引：

[crates/typst-library/src/introspection/introspector.rs:L169-L190](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L169-L190) —— `ElementIntrospector<P>` 的字段就是它的索引清单：`elems: Vec<(Content, P)>`（按文档序的元素+位置表）、`keys`（内容哈希 → locations，测量配对用）、`locations: FxHashMap<Location, Range<usize>>`（**区间索引**：元素自己 + 它的全部概念上的后代占据的数组范围）、`labels`（标签多映射）、`queries: QueryCache`。

builder 如何把事件流变成索引：

[crates/typst-library/src/introspection/introspector.rs:L513-L530](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L513-L530) —— `discover_tag`：`Start` 时若 `flags.introspectable` 且该 `Location` 未见过（`seen` 去重——测量与实排共享同一 `Location`，标签会出现多次，只记一次），压入 `Start` 事件；`End` 时登记 `keys` 并压入 `End` 事件。

[crates/typst-library/src/introspection/introspector.rs:L561-L574](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L561-L574) —— `start_insertion`/`end_insertion`：测量场景的插入件机制。`start` 暂存当前 sink，`end` 把期间攒下的元素挂到 `insertions[parent]`，等待排序时插到父元素区间内。

[crates/typst-library/src/introspection/introspector.rs:L576-L631](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L576-L631) —— `finalize` 消费事件流，`visit` 逐项处理：`Start` 时先在 `locations` 里插入「只覆盖自己」的区间 `idx..idx+1`，登记 `labels`，压入 `elems`，然后**递归处理属于自己的插入件**（L618-L622）；`End` 时把该元素区间的终点改写为当前数组长度（L624-L629）。这一进一出，恰好让每个元素的 `Range` 覆盖自己与全部后代——`Selector::Within`（祖先后代查询）与二分查找都依赖这一点。

查询引擎的策略分派：

[crates/typst-library/src/introspection/introspector.rs:L194-L342](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L194-L342) —— `query` 开头先查缓存（L195-L198），然后按选择器形态分派：

- `Elem(..)`：全表线性过滤（L201-L205）；
- `Label`：直接查标签索引（L209-L214）；
- `Or`：各子查询结果合并进 `BTreeSet<usize>` 按下标**去重并恢复文档顺序**（L215-L222）；
- `And`：取最小的结果集，对其余结果集做二分求交（L223-L245）；
- `Before`/`After`：先算子查询，再用 `binary_search` 在有序结果里切一刀（L246-L273）——能二分的前提正是 `elems` 按文档序排列；
- `Within`：沿祖先的 `loc_range` 区间收集后代（L274-L335）；
- `Can`/`Regex` 在此不支持（L336-L337）。

[crates/typst-library/src/introspection/introspector.rs:L392-L405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L392-L405) —— `query_count_before`：`query(selector).len()` 的优化版，计数器专用（4.4 会用到）。

[crates/typst-library/src/introspection/introspector.rs:L685-L703](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L685-L703) —— `QueryCache`：以选择器哈希为键的 `RwLock` 缓存。字段注释解释了动机：即使顶层查询各不相同，它们常有**共享的子查询**（例如许多个带 `before` 的计数器查询都依赖同一个全局计数查询）。

最后补一个布局侧的细节：文档末尾（最后一页之外）悬空的标签怎么办？

[crates/typst-layout/src/pages/mod.rs:L236-L242](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L236-L242) —— `layout_pages` 把页与页之间收集到的 `tags`（比如恰好在分页点上的元素标签）统一追加到**最后一页末尾**（纵坐标取页高），保证任何可定位元素都不会因为落在页面间隙而丢失。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：通过可观察的查询行为验证「结果按文档顺序排列」与「标签索引」两个论断。

**操作步骤**：

1. 写 `order.typ`：

   ```typ
   = Alpha <a>
   = Beta <b>

   = Gamma <a>
   ```

   （故意让 `<a>` 出现两次，检验 label 多映射与 `query_first`。）

2. 依次执行：

   ```sh
   cargo run -p typst-cli -- eval 'query(heading).map(h => h.body)' --in order.typ
   cargo run -p typst-cli -- eval 'query(<a>).len()' --in order.typ
   cargo run -p typst-cli -- eval 'query(<b>).first().body' --in order.typ
   ```

3. 对照 [introspector.rs:L194-L222](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L194-L222) 阅读：`Elem` 分支是对 `elems` 顺序表的过滤，因此输出顺序即文档顺序；`Label` 分支查 `labels` 多映射，所以重复标签返回多个元素。

**需要观察的现象**：第一个命令按 Alpha、Beta、Gamma 的顺序返回三个标题体；第二个命令返回 `2`；第三个命令返回 `Beta`。

**预期结果**：如上。输出为 JSON 序列化格式（`typst eval` 默认序列化，见 [eval.rs:L159-L191](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/eval.rs#L159-L191)）。具体输出形状**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `elems` 必须严格按文档顺序排列？举两个依赖它的机制。

**答案**：(1) `query` 的语义要求结果按文档中出现的先后排序（目录、编号都依赖这个顺序）；(2) `Selector::Before`/`After` 与 `query_count_before` 用**二分查找**在结果列表里定位切分点（[introspector.rs:L246-L273](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L246-L273)），二分的前提是有序；此外 `Selector::Or` 用 `BTreeSet<usize>` 去重后也要靠下标恢复顺序。

**练习 2**：`locations` 索引为什么存「区间」而不是单个下标？

**答案**：区间 `idx..end` 覆盖元素**及其全部概念上的后代**（在 `visit` 里由 Start/End 事件对维护，[introspector.rs:L598-L631](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L598-L631)）。`Selector::Within { selector, ancestor }` 要找「祖先内部的所有匹配元素」，就是拿祖先的区间去列表里截取（L274-L335）。单下标无法表达包含关系。

**练习 3**：内省器为什么忽略 `FrameItem::Text/Shape/Image`，却又专门收集 `FrameItem::Link`？

**答案**：内省的对象是**语义元素**（由 `Tag` 承载），文本/形状/图像是排版终态图元，查询它们没有意义。`Link` 是例外：`frame_link_targets`（[introspect.rs:L70-L74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L70-L74)）记录文档实际链接到了哪些 `Location`，供导出器判断「链接目标不存在」这类问题——这是排版结果自身携带、别处无从得知的信息。

---

### 4.4 `query` / `counter` 的内省语义

#### 4.4.1 概念说明

前两节解决了「查询什么」（数据库），本节回答「怎么查、查了之后发生什么」。

核心抽象是 `Introspect` trait：**把一次内省请求建模为一个可哈希、可诊断的值对象**。`query(heading)` 在排版期变成一个 `QueryIntrospection(Selector, Span)` 值，交给 `Engine::introspect`：

1. 立即对「本轮持有的上一轮 introspector」执行，拿到结果（哪怕结果是错的/空的）；
2. 同时把这个请求**记录**到 `Sink::introspections`——如果最终没收敛，这些记录就是诊断的原料（4.5）。

这个设计把「读取」和「记账」绑定在一起：编译器不需要理解每种查询的语义，只需要在排完版后检查「所有已记录请求的答案变没变」。

`counter` 在此之上再叠一层语义：**计数器的当前值 = 从文档开头回放到当前位置的全部更新**。每次 `counter.step()/update()` 都会在文档里放一个（不可见的）`CounterUpdateElem`——它本身是 Locatable 的，因此出现在 introspector 里；读取计数器就是查出所有这些更新元素、按文档顺序回放状态机。

#### 4.4.2 核心流程

`query` 的路径：

```text
脚本: #context query(heading)
    ▼ (u3-l7: context 表达式提供 location/styles)
query() 函数体:
    context.introspect()?        # 守卫：没有 context 直接报错
    engine.introspect(QueryIntrospection(selector, span))
        ├─ introspection.introspect(engine, introspector)  # 真正读取
        └─ sink.introspection(请求)                        # 记账
```

`counter` 的路径（以 `counter.at(loc)` 为例）：

```text
counter.select(introspector, loc)
    → 选择器 = 「所有 key 匹配的 CounterUpdateElem」
               Or 「被计数的元素本身」（如全部 heading）
sequence(选择器)                 # 沿文档顺序回放更新，得到 (状态, 页号) 序列
    → 第 i 步状态 = 前 i 次更新后的值；每步还记录所在物理页
query_count_before(选择器, loc)  # loc 之前有几个匹配 → 定位到序列下标
    → 序列[下标] 即该位置的计数器值
（page 计数器额外用物理页差值修正）
```

#### 4.4.3 源码精读

`query` 函数与它的守卫：

[crates/typst-library/src/introspection/query.rs:L159-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L159-L176) —— `query` 是 `#[func(contextual)]`：第一行 `context.introspect()?` 要求至少知道一点上下文（位置或样式），否则报 "can only be used when context is known"；随后 `engine.introspect(QueryIntrospection(target.0, span))`。文档注释（L73-L101）里有个经典反例：查询 heading 的数量再生成同样数量的假 heading——每轮查询结果都在变，永不收敛，Typst 五轮后放弃并警告。

[crates/typst-library/src/foundations/context.rs:L47-L60](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/context.rs#L47-L60) —— `Context::introspect` 守卫：`location` 或 `styles` 任一存在即放行；错误附带提示「try wrapping this in a `context` expression」。这解释了 u3-l7 的设计：`context` 表达式挂起为 `ContextElem`，正是为了在排版期把 location/styles 塞进来。

`Engine::introspect`——执行 + 记账：

[crates/typst-library/src/engine.rs:L109-L117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/engine.rs#L109-L117) —— 两行核心：先 `introspection.introspect(self, introspector)` 拿到输出，再 `self.sink.introspection(Introspection::new(introspection))` 把请求记入 sink。排版结束后 `subsink.introspections()` 就是本轮全部内省请求清单。

`Introspect` trait 的契约：

[crates/typst-library/src/introspection/convergence.rs:L93-L140](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L93-L140) —— 关联类型 `Output: Hash`（**要稳定的就是它**，注释特别提醒：输出的信息量会影响收敛速度——把查询结果压缩成布尔可能早一轮收敛）；`introspect` 执行读取；`diagnose` 在不收敛时生成诊断。文档注释说明了为什么 counter/state 不直接复用 `QueryIntrospection`：为了给用户更贴近直觉的报错文案。

[crates/typst-library/src/introspection/query.rs:L178-L203](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L178-L203) —— `QueryIntrospection`：`Output = EcoVec<Content>`，`introspect` 委托 `introspector.query(&self.0)`；`diagnose` 先比较各轮结果**长度**，长度不稳就说「number of X did not stabilize」，否则说「query for X did not stabilize」。

`counter` 的数据模型：

[crates/typst-library/src/introspection/counter.rs:L217-L230](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L217-L230) —— `Counter(CounterKey)`：键有三种——`Page`（页计数器）、`Selector`（元素计数器，如 `counter(heading)`）、`Str`（自定义计数器，如 `counter("my-counter")`）。

[crates/typst-library/src/introspection/counter.rs:L238-L263](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L238-L263) —— `select`：把键翻译成选择器——先选「所有 key 匹配的 `CounterUpdateElem`」，`Selector` 键再把**被计数的元素本身**用 `Or` 并进来（heading 计数器既要知道你手动 update 过几次，也要数 heading 元素本身出现的次数）。

[crates/typst-library/src/introspection/counter.rs:L661-L684](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L661-L684) —— `CounterUpdateElem`：`#[elem(Construct, Locatable, Count)]`——**计数更新自己就是一种可定位元素**（不可手动构造）。它必须 Locatable，否则 `query_count_before(selector, loc)` 无法按位置切分更新序列。`Count` trait（[L584-L588](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L584-L588)）让特殊元素自定义计数行为（如 heading 按层级 step）。

回放状态机：

[crates/typst-library/src/introspection/counter.rs:L894-L967](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L894-L967) —— `sequence` / `sequence_impl`：从初值（页计数器从 1 开始，见 `CounterState::init`）出发，遍历 `introspector.query(selector)` 的**按文档序**结果，逐个应用更新（没有 `Count` 实现的匹配元素默认 `Step(1)`），每一步记录 `(状态, 物理页号)`。函数带 `#[comemo::memoize]`，注释点明动机：**所有对同一计数器的读取共享一次回放，把复杂度从平方降到线性**。

[crates/typst-library/src/introspection/counter.rs:L788-L819](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L788-L819) —— `CounterAtIntrospection::introspect`：`sequence` 回放 → `query_count_before(selector, loc)` 算出 loc 之前有几个匹配 → `sequence[offset]` 即答案；页计数器额外用「当前物理页 − 序列记录页」的差值补上隐式翻页步进（L805-L812）。

顺带两个相邻机制：

- [crates/typst-library/src/introspection/counter.rs:L726-L786](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L726-L786) —— `ManualPageCounter`：**排版期**（而非查询期）维护的物理/逻辑页计数器。`visit` 扫描每页帧里的 `CounterUpdateElem` 应用显式更新，`step` 在翻页时双计数加一。它在 [pages/mod.rs:L203-L224](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L203-L224) 的 finalize 流程中逐页推进，用来生成 `Page::number`（逻辑页号，见 [document.rs:L117-L119](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L117-L119)）。
- [crates/typst-library/src/introspection/here.rs:L49-L52](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/here.rs#L49-L52) 与 [locate.rs:L27-L42](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locate.rs#L27-L42) —— `here()` 直接从 context 取当前 `Location`；`locate(selector)` 要求选择器唯一匹配并返回其 `Location`（内部走 `QueryUniqueIntrospection`）。`state` 类型的机制与 counter 同构，见同目录 `state.rs`。

#### 4.4.4 代码实践

**实践目标**：亲手用 `query` + `counter` 搭一个迷你目录，体会「查询结果喂给计数器」的完整链路。

**操作步骤**：

1. 写 `manual-toc.typ`（改编自 [query.rs:L31-L65](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L31-L65) 的官方示例）：

   ```typ
   #set page(numbering: "1")
   #heading(outlined: false)[Table of Contents]
   #context {
     let chapters = query(heading.where(level: 1, outlined: true))
     for chapter in chapters {
       let loc = chapter.location()
       let nr = counter(page).display(at: loc)
       [#chapter.body #h(1fr) #nr \ ]
     }
   }

   = Introduction
   #lorem(60)

   = Discussion
   #lorem(60)
   ```

2. 编译：`cargo run -p typst-cli -- compile manual-toc.typ`。

3. 用 `typst eval` 直接查询计数器在某标签处的值（`typst eval` 的求值上下文带样式，因此 `context.introspect()` 守卫放行）：

   ```sh
   cargo run -p typst-cli -- eval 'counter(page).at(<disc>)' --in manual-toc.typ
   ```

   （需要先给 Discussion 标题加 `<disc>` 标签。）

**需要观察的现象**：目录两行的页码正确；即使 Introduction 被目录挤到第 2 页，页码依然正确（多轮迭代生效）。

**预期结果**：第 3 步输出类似 `(2,)` 的数组（counter 值是 `CounterState`，脚本形态为数组）。具体数值取决于分页，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `counter` 的更新要作为元素（`CounterUpdateElem`）放进文档，而不是维护一张全局表？

**答案**：因为「计数器在某位置的值」本质上取决于**文档结构**（哪些更新排在这个位置之前），而文档结构每轮排版都可能变。把更新做成可定位元素后，求值期只是「往文档里放元素」（纯数据，无副作用），真正的计算推迟到查询时沿文档序回放（`sequence`）——这天然与多轮迭代兼容：重排后更新序列变了，回放结果跟着变，收敛判定自动接管。全局表则无法表达「位置」这个维度。

**练习 2**：`query` 为什么必须是 `contextual` 函数（必须在 `context` 里调用）？

**答案**：两个原因。(1) 语义上：查询结果依赖于「排版的当前状态」，在没有位置/样式上下文的地方（如模块顶层）查询语义不明；(2) 机制上：`context.introspect()` 守卫（[context.rs:L48-L50](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/context.rs#L48-L50)）要求 context 至少携带 location 或 styles 之一，而这两样正是 u3-l7 里 `ContextElem` 挂起后在排版期注入的。反例是 `typst eval`：它构造的 context 只有 styles 没有 location，所以 `query` 可用但 `here()` 会报错。

**练习 3**：`query_count_before(selector, end)` 与 `query(selector.before(end, true)).len()` 等价，为什么前者要单独存在？

**答案**：性能。前者复用一次 `query(selector)` 的（缓存的）完整结果，再用二分查找定位切分点（[introspector.rs:L392-L405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L392-L405)）；后者要为每个 `end` 构造新选择器、触发新的（子）查询。计数器对每个读取位置都要做一次「之前有多少个」，这是热路径，trait 注释直接称之为 "An optimized version"。

---

### 4.5 迭代收敛判定、不收敛诊断与性能

#### 4.5.1 概念说明

收敛判定有**两层**：

1. **快路径：comemo 约束验证**。排版时对 introspector 的每次读取都被 `constraint` 记账；排完新版文档后，`constraint.validate(新 introspector)` 保守地回答「本轮读到的所有条目在新文档里是否原样成立」。成立 → 收敛，结束。这是 u4-l1 讲过的 comemo 追踪机制在内省上的应用。
2. **慢路径：`History` 逐轮重放**。只有当快路径失败（且已到迭代上限）时才启动：把本轮记录的每一个内省请求在**六个**introspector（空 + 四份历史 + 最终）上重放一遍，比较最后两个观测值的哈希；不一致的请求生成「did not stabilize」警告。

为什么要两层？[crates/typst-library/src/introspection/convergence.rs:L37-L56](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L37-L56) 的注释给了答案：comemo 看到的数据可能比内省请求**多**（内省可能把数据过滤后再返回），存在「comemo 验证失败但文档实际已收敛」的情况——此时慢路径重放发现无差异，就不再误报警告。

性能视角：多轮迭代听起来昂贵，实际上有三道护栏：

- **求值只跑一次**（4.1）；
- **布局整体被 `#[comemo::memoize]` 包裹**，且 `Location` 的分层哈希设计让「未受扰动的元素」跨轮拿到相同身份证，缓存大面积命中；
- **查询侧有 `QueryCache` 与 memoized `sequence`**，把重复查询/回放压到近线性。

#### 4.5.2 核心流程

快路径（主循环内，每轮一次）：

```text
constraint = Constraint::new()
排版（introspector 以 constraint 追踪）
stable = constraint.validate(新文档的 introspector)
stable == true → break（收敛）
```

慢路径（仅在五轮耗尽时）：

```text
introspectors = [Empty, history[0..4], 最终文档]     # 共 6 个
for 记录在案的每个内省请求 I:
    history = History::compute(在 6 个 introspector 上分别执行 I)
    hash(history[4]) != hash(history[5])  # 第 5 轮 vs 假想的第 6 轮
        → I.diagnose(history) 生成警告（附每轮观测值清单）
汇总为一条 "document did not converge within five attempts" 总警告
```

数学上，设某内省请求在第 \(i\) 轮的观测值为 \(v_i\)（\(i = 1..5\)，另记最终文档上的重放值为 \(v_{\text{final}}\)），收敛条件是：

\[
\mathrm{hash}(v_{5}) = \mathrm{hash}(v_{\text{final}})
\]

即「若再排一轮，答案也不会再变」。用哈希比较而非相等比较是有意的（[crates/typst-library/src/introspection/convergence.rs:L242-L249](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L242-L249) 的注释）：`0.0`（float）与 `0`（int）值相等但类型不同，下一轮可能表现出差异，不应算收敛。

#### 4.5.3 源码精读

快路径的调用点在 4.1.3 已精读（[typst/src/lib.rs:L145](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L145)、[L159-L162](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L159-L162)）。这里看慢路径：

[crates/typst-library/src/introspection/convergence.rs:L24-L70](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L24-L70) —— `analyze`：遍历本轮记录的所有 `Introspection`，逐个 `diagnose`（内部做 History 重放，见下），有产出的收集为警告；最后若存在任何警告，再插入一条汇总警告 "document did not converge within five attempts"，并附帮助链接 hint。L37-L56 的大段注释解释了「为什么 comemo 验证失败却可能没有警告」——即上文两层判定的理由。

[crates/typst-library/src/introspection/convergence.rs:L174-L187](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L174-L187) —— `Bounds::diagnose`（类型擦除后的分发）：`History::compute` 在六个 introspector 上重放请求，`!history.converged()` 时才调用具体类型的 `diagnose` 生成警告。

[crates/typst-library/src/introspection/convergence.rs:L211-L250](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L211-L250) —— `History`：长度固定为 `INSTANCES = MAX_ITERS + 1 = 6` 的数组，每项是 (introspector, 该 introspector 上的观测值)；`compute` 为每个 introspector 临时组装一个 `Engine` 执行请求；`converged()` 比较下标 `MAX_ITERS-1`（第 5 轮用的内省器）与 `MAX_ITERS`（最终文档）上观测值的 `hash128`。

[crates/typst-library/src/introspection/convergence.rs:L267-L280](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L267-L280) —— `History::hint`：把每轮观测值格式化成提示文本——前五轮标 `run 1`…`run 5`，最后一个标 `final`。这是用户在「不收敛」警告里看到的那份「观测历史」的出处。

具体诊断的样例：

[crates/typst-library/src/introspection/location.rs:L392-L417](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L392-L417) —— 位置类内省的 `format_convergence_warning`：先反查该 `Location` 对应的元素名（`query_first(&Selector::Location(loc))`），生成「`{元素} position did not stabilize`」并附「元素创建于这里」的 span hint 与逐轮观测值。

最后看性能护栏的源码证据：

[crates/typst-layout/src/pages/mod.rs:L52-L76](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/mod.rs#L52-L76) —— `layout_document_impl` 带 `#[comemo::memoize]`：整个文档排版是记忆化函数，输入（world、library、introspector、content、styles）不变则直接返回缓存。多轮迭代中真正变化的往往只是 introspector 的少数条目，配合下游各级 memoize，未受影响的大段布局全部命中缓存。

[crates/typst-library/src/introspection/locator.rs:L49-L112](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs#L49-L112) —— `Locator` 的设计说明：为什么用「span + 每层消歧 + 分层哈希」而不是全局计数器——依次否决了三种策略（计数器：轮间漂移 + 编辑不稳定 + 有状态；span+计数器：仍有全局状态），最终方案按执行树逐层哈希：

\[
h_n = \mathrm{hash}(h_{n-1}, k_n)
\]

其中 \(k_n\) 是第 \(n\) 层的本地标识（span + 同 span 消歧计数）。状态完全局部化（可并行）、编辑局部扰动（缓存友好）、跨轮稳定（迭代可对上号）。L107-L112 还说明这一切都在 comemo 追踪下进行：不含可定位元素的内容即使出现在不同位置也能共享布局缓存。

[crates/typst-library/src/introspection/counter.rs:L894-L898](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L894-L898) —— `sequence` 的文档注释：memoization 让同一计数器的全部读取共享一次回放，「从平方降到线性」。

#### 4.5.4 代码实践

**实践目标**：制造一次「不收敛」，观察五轮上限与诊断警告的完整形态。

**操作步骤**：

1. 写 `fake.typ`（即 [query.rs:L91-L101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L91-L101) 文档中的官方反例）：

   ```typ
   = Real
   #context {
     let elems = query(heading)
     let count = elems.len()
     count * [= Fake]
   }
   ```

2. 编译：`cargo run -p typst-cli -- compile fake.typ`，观察终端诊断。

3. 打开 4.1 实践里的计时方法再跑一次（`--timings fake-timings.json`），数 `iter (` 的个数。

4. 把 `count * [= Fake]` 改成固定数量（如 `2 * [= Fake]`），重新编译，确认警告消失。

**需要观察的现象**：第 2 步应出现警告，总警告形如 "document did not converge within five attempts"，下面还有针对查询本身的 "number of headings did not stabilize"，hint 里逐轮列出 `run 1`…`run 5`/`final` 的观测数量（来自 `History::hint`）；第 3 步应能数出 5 个迭代区间；第 4 步警告消失。

**预期结果**：如上。文档仍会输出 PDF，其中 Fake 标题的数量停留在某一轮的值（文档注释说「Typst simply gives up after a few attempts」）。具体警告文案**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`constraint.validate` 通过的标准是什么？它是精确判定吗？

**答案**：标准是「本轮从旧 introspector **实际读取过**的每个条目，在新 introspector 中保持原值」。comemo 只知道「读了什么、读到什么」，不知道「读取的结果如何被使用」，所以它是**保守**的：可能出现「读到的数据变了、但最终文档恰好不变」的假阴性（这时候进入慢路径重放，若 History 显示无差异则不警告），但不会出现假阳性（验证通过就一定安全）。

**练习 2**：`History` 为什么固定重放六个 introspector，而不是五个？

**答案**：六个实例 = 第一轮用的 `EmptyIntrospector` + 中间压入历史的四份文档（`MAX_ITERS - 1`）+ 触发放弃的那份最终文档（见 [typst/src/lib.rs:L164-L170](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L164-L170) 的组装代码）。前五个对应用户五轮排版各自「看到的」文档；最后一个是「假如再排一轮将会看到的」文档。`converged()` 比较的正是这两个：第 5 轮的输入 vs 假想第 6 轮的输入。缺任何一个都无法回答「如果再来一轮会不会变」。

**练习 3**：同样是「同一内容布局多次」，`Locator::split` 与 `Locator::relayout` 有什么区别？这与内省有什么关系？

**答案**：`split` 用于「布局多个**不同**的孩子」——为每个孩子派生独立 `Locator`，各自拿到不同 `Location`（五个 figure 五个号）；`relayout` 用于「同一内容布局**多次**」（典型是测量）——复用同一 `Locator`，各次产出拿到**相同** `Location`，编译器据此识别「这是同一个元素」，查询/计数不会重复计数（见 [locator.rs:L17-L47](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs#L17-L47)）。这就是 4.3 里 `discover_tag` 用 `seen` 按 `Location` 去重、`discover_frame` 处理 `group.parent` 插入件的根源：测量的产出虽然也带着 Tag 进了收集流程，但最终与实排的同一元素合并为一份。

---

## 5. 综合实践

把本讲全部内容串成一个可复现的实验（在仓库根目录操作，全部用 `cargo run -p typst-cli`）：

**任务**：量化「内省依赖 → 多轮迭代」的因果关系，并验证 `divider` 的可定位性。

**步骤**：

1. 准备 `experiment.typ`：

   ```typ
   #set page(numbering: "1")
   #outline()

   = Introduction <intro>
   #lorem(120)

   = Method
   #lorem(40)
   #divider()
   #lorem(40)

   = Conclusion
   As shown in @intro, we are done.
   #lorem(60)
   ```

2. 编译并导出计时：`cargo run -p typst-cli -- compile --timings exp1.json experiment.typ`。在 `exp1.json` 里数 `iter (` 出现的个数，记为 \(N_1\)。
3. 查询 divider：`cargo run -p typst-cli -- eval 'query(divider).len()' --in experiment.typ`，预期 `1`。
4. 查询标题页码：`cargo run -p typst-cli -- eval 'query(heading).map(h => (h.body, h.location().page()))' --in experiment.typ`，对照 PDF 核对页码。
5. 删除 `#outline()` 与 `@intro`（保留 `#divider()` 与正文），另存为 `experiment-min.typ`，同样导出计时，记轮数为 \(N_2\)。
6. 比较 \(N_1\) 与 \(N_2\)，并用一句话解释差异来源（提示：`outline` 读取全部标题的页码；`@intro` 读取目标位置；`divider` 的 query 只发生在 `typst eval` 里，发生在**编译完成后**，不参与迭代）。

**验收标准**：

- \(N_1 \geq 2\)、\(N_2 = 1\)（**待本地验证**；若 `lorem` 文本量导致分页恰好不受目录影响，\(N_1\) 也可能为 2 或 3）；
- 能指出第 2 步的轮数统计依据是 `ITER_NAMES`（[convergence.rs:L17-L18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L17-L18)）与主循环里的 `TimingScope`（[typst/src/lib.rs:L140](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L140)）；
- 能解释为什么 `typst eval` 里的查询不增加编译轮数（[eval.rs:L63-L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/eval.rs#L63-L87)：先 `typst::compile` 完成迭代收敛，再对最终 introspector 求值表达式）。

## 6. 本讲小结

- **多轮排版是内省的代价与解法**：求值只跑一次；排版以「上一轮文档」为输入迭代到不动点，上限 `MAX_ITERS = 5`，不收敛发警告但仍然产出文档（[typst/src/lib.rs:L134-L186](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L134-L186)）。
- **`Location` 是元素实例的身份证**：u128 分层哈希、跨轮稳定；只有 `#[elem(Locatable)]` 标记的语义元素（清单维护在 location.rs 文档里，`divider` 新近加入）或带标签的元素才有。
- **`Tag` 是 `Location` 的物理载体**：realize 的 `prepare` 给元素发证并打上一对 `Start/End` 标签，随布局流进 `Frame`；下一轮的 `PagedIntrospector` 遍历帧收集它们，建成按文档序的元素表 + 区间/标签/哈希三组索引 + 查询缓存。
- **`Introspect` trait 把「读取」变成「可记账的请求」**：`Engine::introspect` 执行请求的同时把它记入 sink；`query` 是对上一轮文档的选择器查询，`counter` 则把更新序列做成可定位元素、沿文档序回放状态机。
- **收敛判定分两层**：comemo `constraint.validate` 是保守快路径；五轮耗尽后 `History` 在六个 introspector 上重放请求、比较末两轮哈希，生成带逐轮观测值的「did not stabilize」警告。
- **性能有三道护栏**：求值一次 + `layout_document_impl` 整体 memoize + `Locator` 稳定哈希保缓存命中；查询侧再有 `QueryCache` 与 memoized `sequence` 兜底。

## 7. 下一步学习建议

本讲之后，第四单元（排版管线）完结。推荐三条继续路线：

1. **横向补全内省家族**：阅读 `crates/typst-library/src/introspection/state.rs`（状态管理，机制与 counter 同构）、`crates/typst-library/src/model/outline.rs` 与 `reference.rs`（目录与交叉引用如何组合 query/counter/locate）、以及 `bibliography` 相关代码（`Location::variant` 的真实用户）。
2. **进入第五单元导出层**：u5-l1（PDF 导出）会消费本讲的 `PagedDocument` 与 `frame_link_targets`（判断链接目标是否存在）；u5-l8（增量编译）会把本讲的 comemo `Constraint`/`memoize` 讲透。
3. **工具链视角**：u5-l6（typst-ide）会展示 IDE 的跳转/补全如何复用 `Location`/`Introspector`；如果你想立刻动手，试着给 [crates/typst-layout/src/introspect.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs) 的 `PagedIntrospector::new` 加一行 `eprintln!` 打印每页发现的 Tag 数量，再跑第 5 节的综合实践，亲眼看看每轮迭代收集到的元素数量如何变化。
