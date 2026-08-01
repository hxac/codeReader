# State 状态机与分组栈

## 1. 本讲目标

上一讲（u1-l3）我们拆开了 `visit()` 的 8 步调度流水线，知道它「按固定顺序尝试 7 道关卡 + 兜底 push」。但所有这些关卡都在反复读写同一个东西——`State`。`visit()` 每命中一条分支，几乎都会改动 `State` 的某个字段：往 `sink` 里 push、往 `groupings` 栈里压入或弹出一个分组、翻转某个布尔标志。可以说，**`State` 就是整条具现化流水线的「可变工作台」**。

本讲就把这个工作台彻底拆开来看。学完本讲，你应该能够：

- 逐字段说出 [`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L86-L108) 的每个字段在 realize 过程中**扮演什么角色、被谁读写**。
- 掌握 `groupings` 栈这一核心数据结构：它是一个**栈式分配、定长容量**的 [`ArrayVec`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L99-L100)，靠「优先级严格递增才能嵌套」的不变量把深度死死卡在 [`MAX_GROUP_NESTING = 3`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1001-L1003)，并理解**为什么恰好是 3**。
- 解释 `outside` / `may_attach` / `saw_parbreak` 这三个布尔标志的语义、何时被置位/复位、谁会读取它们。

本讲是 u2 系列的**地基**：后续讲 show 规则（u2-l2）、分组规则框架（u2-l6/l7）时，都会反复回到 `State` 的这些字段。

## 2. 前置知识

本讲承接 u1-l2 与 u1-l3，假定你已经了解：

- **`realize()` 的整体流程**：搭好 `State` → 调一次 `visit(root)` → `finish()` 收尾 → 返回 `s.sink`（见 u1-l2）。
- **`visit()` 的 8 步流水线与短路语义**（见 u1-l3）：每个元素只被一条分支认领，`visit()` 是唯一递归入口。
- **`Pair<'a> = (&'a Content, StyleChain<'a>)`** 是输出清单的基本单元；`sink: Vec<Pair>` 是输出槽（见 u1-l2）。
- **`RealizationKind`** 五变体决定本次具现化用哪张静态分组规则表（见 u1-l2）。

本讲会新引入两个概念，先在这里通俗解释：

- **状态机（state machine）**：一个持有「当前进度」的可变对象。realize 过程中发生的一切（套了哪些 show 规则、开了哪些分组、是否处在容器外），都记录在 `State` 的字段里。`visit()` 每处理一个元素，就是一次「读取状态 → 做决策 → 改写状态」的状态机迁移。
- **`ArrayVec<T, N>`**：来自 [`arrayvec`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L11) crate 的容器，可以理解成「容量固定为 N、存在栈上、不分配堆内存的 `Vec`」。它能 `push` / `pop` / `last`，用法和 `Vec` 几乎一样，但容量在编译期就定死；一旦 `push` 超过 N 就会 **panic**。在本讲里它被用来装「当前活跃的分组」，N = 3。

## 3. 本讲源码地图

本讲主要在一个文件里打转，顺手引用邻 crate 的类型定义：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | typst-realize 的全部主逻辑。`State`、`Grouping`、`MAX_GROUP_NESTING`、`visit_grouping_rules` 与四张规则表都在这里。 |
| [src/lib.rs（`realize()` 入口）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43-L74) | 第 51–68 行搭建 `State` 初值，是本讲「字段从哪来」的起点。 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L153-L196) | `RealizationKind`、`Arenas`、`Pair` 的定义，`State` 的若干字段类型来自这里。 |

> 链接基准 HEAD：`32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。下面所有永久链接均基于此 HEAD。

## 4. 核心概念与源码讲解

### 4.1 State 结构体：realize 的可变状态机

#### 4.1.1 概念说明

把 `realize()` 想象成一名厨师做一道菜：食材（`content`）和菜谱（`styles`）是输入，成品（`Vec<Pair>`）是输出。但在烹饪过程中，厨师需要一个**工作台**来摆放半成品、记录火候、记住「现在锅里炖着哪几道」。`State` 就是这个工作台。

它有几点关键性质：

- **全局唯一**：一次 `realize()` 只构造一个 `State`，整个 `visit()` 递归过程都通过 `&mut State` 共享它（`visit` 的第一个参数就是 `s: &mut State`）。
- **只读输入 + 可变工作区**：`engine`/`locator`/`arenas`/`kind`/`rules` 是「配置型」字段，初值由 `realize()` 一次性设定后基本只读；`sink`/`groupings`/三个布尔标志才是「被不断改写」的工作区。
- **返回值就藏在里面**：`realize()` 最后 `Ok(s.sink)`，把工作台上的成品端出去。

#### 4.1.2 核心流程

`State` 的一生分三段：

```
构造（realize L51-L68）
  ├── 根据 kind 选规则表 rules
  ├── sink = 空 Vec
  ├── groupings = 空 ArrayVec
  ├── outside = (kind 是否 Document)
  ├── may_attach = false
  └── saw_parbreak = false

工作中（visit / visit_*_rules / visit_styled / prepare 反复读写）
  ├── sink：每件 well-known 元素 push 进来
  ├── groupings：分组开始时 push、结束时 pop
  ├── outside：进入/离开 show 规则笼子、遇到页面样式时翻转
  ├── may_attach：每遇到 ParElem 置 true、遇到 ParbreakElem 置 false
  └── saw_parbreak：遇到段落断点时置 true

收尾（finish）
  ├── 把未关闭的分组依次 finish
  └──（返回 s.sink）
```

下表把每个字段归类到「配置型 / 工作区」，并标注它的职责：

| 字段 | 类型 | 类别 | 一句话职责 |
| --- | --- | --- | --- |
| `kind` | `RealizationKind<'x>` | 配置 | 本次具现化的「场景」，决定用哪张规则表、kind 规则如何变换 |
| `engine` | `&'x mut Engine<'y>` | 配置 | 编译引擎，用于跑 show 规则、合成、报错、延迟错误 |
| `locator` | `&'x mut SplitLocator<'z>` | 配置 | 给 locatable/labelled 元素分配唯一 `Location` |
| `arenas` | `&'a Arenas` | 配置 | 临时内存池，把 show 规则新产出的 content/styles 生命期延长到 `'a` |
| `rules` | `&'x [&'x GroupingRule]` | 配置 | 本次适用的分组规则表（静态） |
| `sink` | `Vec<Pair<'a>>` | 工作区 | **输出槽**，所有最终元素的归宿，也是 `realize()` 的返回值 |
| `groupings` | `ArrayVec<Grouping<'x>, 3>` | 工作区 | **当前活跃的分组栈**，本讲下半场的主角 |
| `outside` | `bool` | 工作区 | 是否正处在「任何容器/show 规则之外」，决定页面样式能否提升到 page 层 |
| `may_attach` | `bool` | 工作区 | 紧随其后的 attach 垂直间距是否应当保留 |
| `saw_parbreak` | `bool` | 工作区 | 是否见过段落断点，用于 Fragment 的 inline 回退判定 |

#### 4.1.3 源码精读

先看 `State` 的定义与它上方那段关于「为什么需要这么多生命周期」的注释：

[src/lib.rs:L76-L108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L76-L108) —— `State` 结构体。注释解释了为什么要四个生命周期参数：因为 `&mut` 引用是**不变（invariant）**的，若 `engine` 和 `locator` 共享一个生命周期，不变性会强制它们的生命期完全相等，反而限制调用方。真正的「有意义」生命期只有 `'a`——它是进出 `realize()` 的 content 的生命期。

四个生命期对应如下（不必死记，深入讨论留到 u3-l3）：

- `'a`：content / arenas / sink 里元素的生命期，与 `fn realize` 上的 `'a` 相同，是唯一对外可见的生命期。
- `'x`：`engine` / `locator` / `rules` / `kind` / `groupings` 这些**借用**的生命期。
- `'y`：`Engine` 内部持有的生命期（`Engine<'y>`）。
- `'z`：`SplitLocator` 内部持有的生命期（`SplitLocator<'z>`）。

接着逐组看字段。**配置型字段**的初值在 `realize()` 里一次性设定：

[src/lib.rs:L51-L68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L51-L68) —— `State` 的构造。注意三个细节：

- `rules` 用 `match kind` 在四张静态表里选一张（L55–L61）。
- `outside` 初值**仅**当 `kind == Document` 时为 `true`（L64）：只有文档级具现化才「一开始就在容器外」。
- `may_attach`、`saw_parbreak` 恒以 `false` 起步（L65–L66）。

`kind` / `engine` / `locator` / `arenas` 的类型定义在邻 crate：

[crates/typst-library/src/routines.rs:L153-L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L153-L169) —— `RealizationKind`，其中 `Document` 与 `Fragment` 各持有一个 `&mut`，用于回填 `DocumentInfo` 与 `FragmentKind`。

[crates/typst-library/src/routines.rs:L182-L196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L182-L196) —— `Arenas`（三个 arena：content / styles / bump）与 `Pair` 类型别名。

**工作区字段**中，`sink` 最直白——它就是输出清单：

[src/lib.rs:L95-L96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L95-L96) —— `sink: Vec<Pair<'a>>`，`visit()` 的兜底 push（L291）和所有分组的暂存都写到这里，最后由 `Ok(s.sink)`（L73）返回。

`arenas` 配合两个辅助方法 `store` / `store_slice`，用来把短命的新内容「续命」到 `'a`：

[src/lib.rs:L204-L220](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L204-L220) —— `store` 把一个 `Content` 分配进 typed arena 返回 `&'a Content`；`store_slice` 用 `BumpVec` 复制一批 pair（注释解释了为何用 `BumpVec` 而非 `alloc_slice_copy`：便于在 drop 时复用空间）。这两个方法是 `State` 把 show/分组产出的临时内容「挂」到长生命期的渠道。

> `groupings`、三个布尔标志的字段级精读放到 4.2 与 4.3 专门展开，避免本节过载。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 填表」的方式，确认每个 `State` 字段被哪些函数读写，建立字段与调用点的一一对应（无需编译）。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs)。
2. 在编辑器里搜索 `s.kind`、`s.sink`、`s.groupings`、`s.outside`、`s.may_attach`、`s.saw_parbreak`、`s.rules`、`s.arenas`、`s.locator`、`s.engine`，逐个记录命中位置所在的函数名。
3. 把结果填进下表（「写」= 赋值或 push/pop，「读」= 仅取值）：

   | 字段 | 写它的函数（举例） | 读它的函数（举例） |
   | --- | --- | --- |
   | `sink` | `visit`（兜底 push）、`visit_grouping_rules` | `finish_*`、`Grouped::get` |
   | `groupings` | （待你填） | （待你填） |
   | `outside` | （待你填） | （待你填） |
   | `may_attach` | （待你填） | （待你填） |
   | `saw_parbreak` | （待你填） | （待你填） |
   | `kind` | （只有 `realize` 初值，基本只读） | `visit_kind_rules`、`visit_styled`、`visit_filter_rules`、`finish` |

**需要观察的现象**：你会发现 `kind` / `rules` / `arenas` / `engine` / `locator` 几乎**只被读**（初值除外），而 `sink` / `groupings` / 三个布尔标志才是被频繁改写的工作区。

**预期结果**：上表应能填成「写它的函数」与「读它的函数」两列都非空，且工作区字段的写点明显多于配置字段。这正好印证 4.1.2 的「配置型 vs 工作区」划分。

#### 4.1.5 小练习与答案

**练习 1**：`State` 为什么要做成一个**集中式**的可变结构，而不是把 `sink`、`groupings` 等作为独立参数一路透传给 `visit()`？

> **参考答案**：集中式结构让 `visit()` 的签名保持简洁（只收 `&mut State` + content + styles），新增一个状态字段不必改动所有调用点；同时也让「状态迁移」集中可见，便于推理。代价是字段较多、需要文档辅助理解——这正是本讲的存在意义。

**练习 2**：`sink` 的类型是 `Vec<Pair<'a>>`，而 `groupings` 用的是 `ArrayVec<…, 3>`。为什么输出槽用堆分配的 `Vec`，分组栈却用栈分配的 `ArrayVec`？

> **参考答案**：输出元素数量不可预估（可能成千上万），只能用能动态增长的 `Vec`；而分组栈的深度有**编译期已知的小上界**（3），用定长 `ArrayVec` 既能避免每次 realize 都做堆分配，又能把「深度绝不超过 3」这一不变量固化进类型——一旦逻辑出错导致越界，会直接 panic 暴露 bug，而非静默地继续。

---

### 4.2 groupings 分组栈：Grouping 与 MAX_GROUP_NESTING

#### 4.2.1 概念说明

「分组（grouping）」是把若干连续的叶子元素收集起来、时机成熟时合并成一个复合元素的过程——比如把一段行内文字收成 `ParElem`，把若干列表项收成 `ListElem`。一个分组一旦开始，就处于「活跃」状态，持续吞入新元素，直到被某个元素「打断」或整体收尾时才 `finish`。

问题在于：分组可以**嵌套**。例如一段文字里夹着一条引用（`CiteElem`），外层是段落分组，引用自己又是一个引用分组，引用内部的文字又会被文本分组收纳。为了表达「当前同时开着哪几层分组、谁套在谁里面」，realize 用一个**栈**来管理活跃分组——栈顶是最内层分组，最先 finish。

栈里每个元素是一个 [`Grouping`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L149-L161) 结构体，记录这一层分组的元数据；整个栈则是 [`State.groupings`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L99-L100)，一个容量为 3 的 `ArrayVec`。

为什么容量是 3？因为分组规则被赋予了**优先级（priority）**，而嵌套只允许「更高优先级套在更低优先级里」。本 crate 的规则只有 3 档互不相同的优先级（1、2、3），所以最多嵌套 3 层——这就是 [`MAX_GROUP_NESTING = 3`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1001-L1003) 的由来。源码注释说得直白：*Corresponds to the number of unique priority levels.*

#### 4.2.2 核心流程

分组栈的压入与弹出都发生在 `visit_grouping_rules`（即 u1-l3 的步骤 6）。它的判定逻辑可概括为：

```
visit_grouping_rules(content):
  matching = 规则表里第一条对 content 返回 Trigger 的规则

  while 栈非空（看栈顶 active）:
      # (A) 若新匹配规则的优先级 > 栈顶 → 准备开更内层的分组，先 break
      if matching 存在 且 matching.priority > active.priority:
          break

      # (B) 否则，若 content 能并入栈顶分组 → 直接 push 进 sink，返回 true
      effect = active.rule.effect(content)
      if not active.interrupted 且 effect != Interrupt:
          active.contains_neutral |= (effect == Neutral)
          sink.push(content)
          return true

      # (C) 否则 → finish 掉栈顶，继续循环看下一层（含 512 次防死循环守卫）
      finish_innermost_grouping()
      i += 1;  if i > 512: bail "maximum grouping depth exceeded"

  # 循环结束后，若确实有 matching → 开新分组压栈，push，返回 true
  if let Some(rule) = matching:
      groupings.push(Grouping { start: sink.len(), rule, interrupted:false, contains_neutral:false })
      sink.push(content)
      return true

  return false   # 没有任何分组规则对它感兴趣
```

关键的**深度不变量**在 (A)：只有当 `matching.priority > active.priority`（严格大于）时才会「break 去开新分组」，从而把更高优先级套进更低优先级**之内**。由于：

- 优先级只有 {1, 2, 3} 三档；
- 每多嵌套一层，优先级必须**严格递增**；

所以一条嵌套链最多是 `优先级1 ⊃ 优先级2 ⊃ 优先级3`，深度上限就是 3。`ArrayVec` 的容量 3 正好匹配：既不浪费，又能在万一打破不变量时以 panic 兜底。

把各规则的优先级列出来对照（取自 4.2.3 的源码）：

| 规则 | priority | tags | 用在哪些 kind |
| --- | --- | --- | --- |
| `TEXTUAL`（文本分组，套正则 show 规则） | 3 | true | Flow、Par |
| `CITES`（引用分组） | 2 | false | Flow、Par、Math |
| `LIST` / `ENUM` / `TERMS`（三类列表） | 2 | false | Flow、Par、Math |
| `PAR`（段落分组） | 1 | true | 仅 Flow |

> 注意 `CITES` 与三类列表都是优先级 2——**相同优先级不会互相嵌套**，遇到对方时会先 finish 当前再开新的（走 (C) 路径），所以它们是「平级互斥」的关系。

#### 4.2.3 源码精读

先看 `groupings` 字段的声明——注意它的类型把容量上限直接写进了签名：

[src/lib.rs:L99-L100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L99-L100) —— `groupings: ArrayVec<Grouping<'x>, MAX_GROUP_NESTING>`。`Grouping<'x>` 是栈元素类型（其 `'x` 来自所引用的 `&'x GroupingRule`）。

栈元素 `Grouping` 只有两个 usize 加两个 bool 加一个规则引用，非常紧凑：

[src/lib.rs:L149-L161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L149-L161) —— `Grouping` 结构体。四个字段：

- `start: usize`：本层分组在 `sink` 中的起始下标。`finish` 时只需对 `sink[start..]` 这段做合并。
- `interrupted: bool`：**仅对 PAR 有效**。段落分组被打断、但尚未 finish（因为可能因「全是行内内容」而被整体忽略，见 `is_fully_inline_or_neutral`）。
- `contains_neutral: bool`：本层是否吞入过 neutral 元素（用于混合 inline/block 的 HTML，finish 时要把 neutral 段切开分别处理）。
- `rule: &'a GroupingRule`：驱动本层的规则（含 priority / effect / finish 函数指针）。

再看容量常量与它「对应优先级档数」的注释：

[src/lib.rs:L1001-L1003](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1001-L1003) —— `const MAX_GROUP_NESTING: usize = 3;`，注释明确：*Corresponds to the number of unique priority levels.*

各规则的优先级确实只有 1/2/3 三档。摘取 `TEXTUAL`（最高 3）和 `PAR`（最低 1）对照：

[src/lib.rs:L1018-L1041](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1018-L1041) —— `TEXTUAL` 规则，`priority: 3`（L1019）。它对 `TextElem`/`LinebreakElem`/`SmartQuoteElem` 返回 `Trigger`，对空格返回 `Inner`，其余返回 `Interrupt`。

[src/lib.rs:L1044-L1071](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1044-L1071) —— `PAR` 规则，`priority: 1`（L1045）。它把行内元素收成段落。

[src/lib.rs:L1074-L1091](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1074-L1091) —— `CITES` 规则，`priority: 2`（L1075）。

[src/lib.rs:L1103-L1120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1103-L1120) —— `list_like_grouping` 泛型，统一生成 `LIST`/`ENUM`/`TERMS`，三者都是 `priority: 2`（L1105）。

> 于是唯一的三档优先级是 {3, 2, 1}，正好对应 `MAX_GROUP_NESTING = 3`。

现在看驱动栈的 `visit_grouping_rules`，重点看 (A) 的「严格大于才嵌套」与 (C) 的 512 守卫：

[src/lib.rs:L694-L749](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L694-L749) —— 整个函数。三段对应 4.2.2 的 (A)/(B)/(C)：

[src/lib.rs:L708-L712](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L708-L712) —— (A) 核心不变量：`matching.priority > active.priority` 才 `break` 去开嵌套分组。正是这一句保证了「深度 ≤ 优先级档数」。

[src/lib.rs:L714-L720](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L714-L720) —— (B) 元素并入栈顶分组：只要没被打断且 effect 不是 `Interrupt`，就 `push` 进 `sink`（注意：分组期间元素**暂存**在 `sink` 的一个区间里，并不立即输出），并顺手记录是否吞了 neutral 元素。

[src/lib.rs:L722-L732](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L722-L732) —— (C) `finish` 栈顶后循环，附带 512 次迭代上限。注释说明这个上限主要防「show 规则与分组规则互相喂料」形成的循环（详见 u3-l6）。

最后看「开新分组」时如何构造 `Grouping` 并压栈——`start` 取当前 `sink` 长度：

[src/lib.rs:L735-L746](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L735-L746) —— 把 trigger 元素本身作为分组第一个元素 push 进 `sink`，并在 `groupings` 压入一个新 `Grouping`。

#### 4.2.4 代码实践

**实践目标**：用插桩亲眼看到 `groupings` 栈的压入/弹出与最大深度，验证「深度被卡在 ≤ 3」。

**操作步骤**：

1. 构建一次 CLI（若 4.1.4 未构建过）：

   ```bash
   cargo build -p typst-cli
   ```

2. 打开 `crates/typst-realize/src/lib.rs`，在 `visit_grouping_rules` 里给压栈和弹栈各加一行日志（示例代码，仅供学习，验证后请还原）：

   ```rust
   // 在 L738 s.groupings.push(Grouping { ... }); 这一句之后加：
   eprintln!("[group] PUSH depth={} rule_priority={}", s.groupings.len(), rule.priority);

   // 在 finish_innermost_grouping 的 s.groupings.pop().unwrap()（L855）之后加：
   eprintln!("[group] POP  depth={}", s.groupings.len());
   ```

3. 准备一个段落里夹引用的文档 `doc.typ`（引用会触发 CITES 分组，外层文字会落在 PAR/TEXTUAL 上，理论上能让单次 realize 触碰多档优先级）：

   ```typ
   Hello #cite<nietzsche> world
   #bibliography("x.bib")
   ```

   > 若没有 `.bib` 文件不方便运行，可退而用一个更简单的、不含引用的文档 `Hello world`，先观察 TEXTUAL/PAR 两档的压弹。

4. 运行并把 stderr 存盘：

   ```bash
   cargo run -p typst-cli -- compile doc.typ 2> group.log
   ```

5. 统计日志里出现过的最大 `depth`：

   ```bash
   grep -o 'depth=[0-9]' group.log | sort | uniq -c
   ```

**需要观察的现象**：

- 日志会出现多组 PUSH/POP，对应排版过程中多次 `realize()` 调用（flow / inline / pages 各自调用，正常现象）。
- `depth` 的取值应在 `1..=3` 之间，**绝不超过 3**。

**预期结果**：

- 简单文档 `Hello world` 多半只看到 `depth=1`（TEXTUAL）或 `depth=2`（TEXTUAL 套在 PAR 内）。
- 含引用的段落有机会看到 `depth` 触及 3（PAR(1) ⊃ CITES(2) ⊃ TEXTUAL(3)）。
- **精确的最大深度待本地验证**：它取决于内容到达 `visit_grouping_rules` 时的具体顺序与是否已建立 PAR 分组；但**无论内容多复杂，depth 都不会超过 3**——这正是 `MAX_GROUP_NESTING` 与优先级不变量共同保证的。

> 若你尝试构造「三层嵌套列表」`- A\n  - B\n    - C`，会发现 `depth` 并不因此变成 3：列表项的**正文**是在排版阶段由**另一次嵌套 `realize()`（Fragment kind）**处理的，跨列表层级的嵌套发生在「多次 realize 调用」之间，而非单次调用的 `groupings` 栈里。单次调用内的栈深仍由上面的三档优先级决定。

#### 4.2.5 小练习与答案

**练习 1**：假如把 `TEXTUAL` 的 `priority` 从 3 改成 2（与 `CITES` 相同），`MAX_GROUP_NESTING = 3` 还合理吗？会发生什么？

> **参考答案**：不再合理。此时唯一优先级只剩 {1, 2} 两档，理论最大嵌套深度降到 2，`MAX_GROUP_NESTING = 3` 会偏大（虽不会 panic，但浪费一个槽位）。更重要的是，`TEXTUAL` 与 `CITES` 同优先级后将**互相打断**而非嵌套，原本「引用内部的文字被文本分组收纳」的语义会改变——这正说明优先级数值是分组行为的核心开关。

**练习 2**：`Grouping.start` 为什么存的是「`sink` 的下标」而不是「直接存这一层的元素列表」？

> **参考答案**：因为分组期间的元素本来就**暂存在 `sink` 的连续区间**里（见 4.2.3 的 (B)）。用一个下标 `start` 圈出 `sink[start..]` 这段，就免去了把元素在 `sink` 和「分组内部列表」之间来回搬运的开销；`finish` 时直接对这段切片操作即可。这是一种「就地暂存」的设计。

**练习 3**：`visit_grouping_rules` 里 (C) 路径的 512 次迭代上限，和 `MAX_GROUP_NESTING = 3` 是同一道防线吗？

> **参考答案**：不是，它们防的是两种不同的失控。`MAX_GROUP_NESTING` 防的是**单次调用内**的分组嵌套过深（由优先级不变量保证 ≤ 3，`ArrayVec` 容量兜底）；512 上限防的是 `finish` 产出新内容、新内容又触发新分组的**循环**（典型成因是 show 规则与分组规则互相喂料）。两道防线的关系与触发场景详见 u3-l6。

---

### 4.3 三个布尔标志：outside / may_attach / saw_parbreak

#### 4.3.1 概念说明

除了 `sink` 和 `groupings`，`State` 还有三个不起眼但至关重要的布尔标志。它们都是「**记住某件刚刚发生过的事，用以影响后续决策**」的一比特状态：

- **`outside`**：现在是否处在「任何容器或 show 规则输出**之外**」？只有 `Document` 级具现化才以 `true` 起步。它的作用是判断「页面样式（`set page(...)`）能否被提升到 page 层」——只有真正在文档顶层、未被任何 show 规则笼子罩住时，页面样式才有资格提升。
- **`may_attach`**：紧跟着的「attach」垂直间距（`VElem` with `attach`）是否应当保留？只有当上一个落地元素是 `ParElem`（即紧跟一个段落）时，attach 间距才有意义；否则会被折叠掉。
- **`saw_parbreak`**：本次具现化中是否出现过段落断点（空行）？它在 Fragment 具现化里用于一个优化判定：若一个片段全是行内内容、且没见过段落断点，就不必强行收成段落（`is_fully_inline_or_neutral`）。

#### 4.3.2 核心流程

三个标志的置位/复位时机各不相同：

```
outside:
  初值  = (kind == Document)              # realize L64
  进入某元素的 show 规则输出时:  outside &= 元素是否 ContextElem   # visit_show_rules L417-L418
  从该 show 规则返回时:          outside = prev_outside           # visit_show_rules L424
  遇到页面样式「突破笼子」时:    outside = true                   # visit_styled L638
  读取点: visit_styled 里 if outside { 把样式标记为 outside() }    # visit_styled L658-L660

may_attach:
  初值  = false                            # realize L65
  遇到 ParbreakElem:        may_attach = false           # visit_filter_rules L769
  兜底（每处理一个非过滤元素）: may_attach = (content 是否 ParElem)  # visit_filter_rules L782
  读取点: 决定 attach VElem 是否被折叠      # visit_filter_rules L772-L779

saw_parbreak:
  初值  = false                            # realize L66
  遇到 ParbreakElem:        saw_parbreak = true           # visit_filter_rules L770
  读取点: is_fully_inline_or_neutral 的 Fragment inline 回退判定   # L1175
```

一句话总结：`outside` 描述「我在文档的哪一层」，`may_attach` 描述「上一个落地的家伙是不是段落」，`saw_parbreak` 描述「这段内容里有没有空行」。

#### 4.3.3 源码精读

先看三个字段在 `State` 里的声明与注释：

[src/lib.rs:L101-L107](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L101-L107) —— `outside` / `may_attach` / `saw_parbreak`，每个字段都带一行精确的文档注释。

初值在 `realize()` 里设定：

[src/lib.rs:L64-L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L64-L66) —— `outside` 仅 `Document` 为 `true`；`may_attach`、`saw_parbreak` 恒为 `false`。

**`outside` 的流转**最复杂。它在 `visit_show_rules` 里随 show 规则的进出而「入笼/出笼」：

[src/lib.rs:L417-L418](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L417-L418) —— 进入一个元素的 show 输出前，先把旧值存到 `prev_outside`，再用 `&=` 让 `outside` 仅当元素是 `ContextElem` 时才保持 true（即：show 规则的产出默认「不在外层」）。

[src/lib.rs:L424](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L424) —— show 规则返回后恢复 `outside = prev_outside`，保证「笼子」只罩住这次 show 输出。

而在 `visit_styled` 里，遇到页面样式时 `outside` 会被显式置 true，随后被读取以决定是否提升样式：

[src/lib.rs:L636-L639](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L636-L639) —— 当 `Document` + `Paged` 目标下出现页面样式，`pagebreak = true` 且 `s.outside = true`（「突破 show 规则笼子」）。

[src/lib.rs:L656-L660](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L656-L660) —— 读取点：若 `outside`，则把本地样式转换成 `outside()` 形式，允许它们在排版时被提升到 page 层。

**`may_attach` 与 `saw_parbreak`** 都在 `visit_filter_rules` 里维护：

[src/lib.rs:L762-L779](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L762-L779) —— `ParbreakElem` 同时把 `may_attach = false`、`saw_parbreak = true`；attach 间距在 `!may_attach` 时被折叠丢弃。

[src/lib.rs:L781-L782](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L781-L782) —— 兜底：每放过一个元素，就用「它是不是 `ParElem`」更新 `may_attach`。所以紧跟段落之后的 attach 间距能存活，其它位置的被折叠。

`saw_parbreak` 的读取点在 Fragment 的 inline 回退判定里：

[src/lib.rs:L1170-L1186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1170-L1186) —— `is_fully_inline_or_neutral`：仅当 `Fragment` 且 `!saw_parbreak` 且唯一的活跃分组是覆盖整个 `sink` 的 PAR 时，才认定片段「全是行内/neutral」，从而在 `finish` 里把 `FragmentKind` 改写成 `Inline`（见 [L788-L802](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L802)）。

#### 4.3.4 代码实践

**实践目标**：用插桩追踪三个标志的翻转，验证它们与具体内容元素的对应关系。

**操作步骤**：

1. 在 `visit_filter_rules` 末尾（L784 `Ok(false)` 之前）加日志：

   ```rust
   eprintln!("[flag] may_attach={} saw_parbreak={} (after {:?})",
       s.may_attach, s.saw_parbreak, content.elem().name());
   ```

   并在 `visit_styled` 读取 `outside` 的分支（L658）加：

   ```rust
   if s.outside { eprintln!("[flag] outside=true → 提升页面样式"); }
   ```

2. 准备一个含「段落 + 空行 + 段落」与一个 attach 间距的文档 `doc.typ`：

   ```typ
   First paragraph.

   Second paragraph.
   #v(1em, weak: true)
   ```

3. 运行 `cargo run -p typst-cli -- compile doc.typ 2> flag.log`，观察 `[flag]` 行。

**需要观察的现象**：

- 遇到空行（`ParbreakElem`）时，`saw_parbreak` 翻成 `true`，`may_attach` 翻成 `false`。
- 每放过一个 `ParElem` 后，`may_attach` 翻成 `true`；放过其它元素后翻成 `false`。

**预期结果**（定性，**精确序列待本地验证**）：

- `saw_parbreak` 一旦在某次 realize 里变 `true` 就不会回退（它记录的是「是否见过」）。
- `may_attach` 像一个「上一个元素是不是段落」的滑动标志，随每个元素起伏。
- `outside=true → 提升页面样式` 只在文档顶层出现 `set page(...)` 时打印。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `outside` 在进入 show 规则输出时要被「压低」，返回时又要恢复？

> **参考答案**：show 规则的产出位于「某个元素内部」，逻辑上不再处于文档顶层，所以页面样式此时不应被提升——`outside &= content.is::<ContextElem>()` 把它压低（`ContextElem` 是例外，它本身代表「透传上下文」）。返回时恢复 `prev_outside`，保证 show 规则结束后，外层的 `outside` 状态不被这次调用污染。这是一种典型的「保存—修改—恢复」栈式纪律。

**练习 2**：如果把 `saw_parbreak` 这个字段删掉、`is_fully_inline_or_neutral` 永远按 `false` 处理，会对用户可见行为产生什么影响？

> **参考答案**：Fragment 具现化将无法做 inline 回退——即使一个容器（如 `box`、`html.span`）里全是连续行内文字、没有空行，也会被强行收成一个 `ParElem`，导致本来该是行内的片段被当成块级处理，排版结果变差（行内容器里冒出一个段落）。`saw_parbreak` 正是区分「真有段落分界的块级片段」与「纯行内片段」的依据。

**练习 3**：`may_attach` 的更新放在 `visit_filter_rules` 的**末尾兜底处**（L782），而不是每条分支里都写一遍。这样设计的好处是什么？

> **参考答案**：被过滤掉的元素（空格、parbreak、折叠的 attach）有自己的 `may_attach` 处理（parbreak 显式置 false），而所有「正常放过」的元素共用末尾这一句统一更新，避免在每个 `return Ok(false)` 前重复书写。集中更新既减少遗漏，也让「放过的最后一个元素是不是 ParElem」这一语义更清晰。

## 5. 综合实践

把本讲的三块（字段全景、分组栈、三个标志）串起来，做一次「画图 + 预测 + 验证」。

**任务**：为 `State` 画一张「字段在 realize 过程中如何变化」的示意图，并用一个三层嵌套列表文档验证 `groupings` 栈深度的行为。

**操作步骤**：

1. **画示意图**。在纸上或文档里画出 `State` 的字段，并用箭头标注：
   - 哪些字段是**配置型**（`realize` 设一次后只读）：`kind`/`engine`/`locator`/`arenas`/`rules`。
   - 哪些是**工作区**（被反复改写）：`sink`/`groupings`/`outside`/`may_attach`/`saw_parbreak`。
   - 对每个工作区字段，标出它的主要写点（参考 4.1.4 填好的表）。

2. **预测**。准备三层嵌套列表 `nest.typ`：

   ```typ
   - outer
     - middle
       - inner
   ```

   基于本讲学到的两点预测：
   - (i) `MAX_GROUP_NESTING = 3`，单次 `realize()` 内 `groupings` 栈深 ≤ 3；
   - (ii) 列表项的正文由**嵌套的 Fragment realize** 处理，所以「三层列表」的层级嵌套会体现在**多次** `[realize]` 调用上，而非单次调用的栈深上。

3. **验证**。复用 4.2.4 的 PUSH/POP 插桩，运行：

   ```bash
   cargo run -p typst-cli -- compile nest.typ 2> nest.log
   ```

   然后统计：
   - 总共有多少组 PUSH/POP（粗略对应多少次嵌套 realize 调用）；
   - 单组里的最大 `depth`（应 ≤ 3）。

**预期结果**（定性，**精确计数待本地验证**）：

- 你会看到**不止一组** PUSH/POP 序列：每一层列表的正文都会触发自己的 realize，印证「列表层级跨调用嵌套」。
- 任意一组的 `depth` 都不会超过 3，印证「单调用内栈深由三档优先级封顶」。
- 配合 4.3.4 的标志插桩，你还应能在含空行的列表项里观察到 `saw_parbreak` 翻转。

> 这道综合实践的关键收获是分清两种「嵌套」：**分组栈内的优先级嵌套**（单调用，≤ 3）与**列表/容器正文跨 realize 调用的嵌套**（无上述上限，但每次调用各自独立）。混淆这两者是初读 realize 时最常见的误区。

## 6. 本讲小结

- [`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L86-L108) 是 realize 的**唯一可变工作台**：配置型字段（`kind`/`engine`/`locator`/`arenas`/`rules`）由 `realize()` 一次性设定后基本只读；工作区字段（`sink`/`groupings`/`outside`/`may_attach`/`saw_parbreak`）被 `visit` 全家桶反复改写。`realize()` 最后 `Ok(s.sink)` 把成品端出。
- `State` 带四个生命期参数（`'a/'x/'y/'z`）是为了绕开 `&mut` 的不变性；唯一对外有意义的是 `'a`（content 的生命期）。深入讨论留待 u3-l3。
- `groupings` 是一个容量为 [`MAX_GROUP_NESTING = 3`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1001-L1003) 的 `ArrayVec`——栈式分配、零堆分配。每个栈元素是一个 [`Grouping`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L149-L161)，记录分组的 `start` 下标、interrupted/contains_neutral 标志与所用规则。
- 深度被卡在 3 的根因是**优先级不变量**：分组规则只有 {1, 2, 3} 三档优先级，而 [`visit_grouping_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L694-L749) 只在 `matching.priority > active.priority` 时才开更内层分组，故嵌套链最多 `1 ⊃ 2 ⊃ 3`。`ArrayVec` 容量既是精确预算，也是越界 panic 的安全网。
- 三个布尔标志各司其职：`outside` 记录「是否在文档顶层」以决定页面样式能否提升；`may_attach` 记录「上一个落地元素是否段落」以决定 attach 间距存活；`saw_parbreak` 记录「是否见过空行」以驱动 Fragment 的 inline 回退。
- 要区分两种嵌套：**分组栈内的优先级嵌套**（单次 realize，≤ 3）与**列表/容器正文跨 realize 调用的嵌套**（每次调用各自独立的 `State`）。

## 7. 下一步学习建议

`State` 的字段已盘点完毕，接下来可以顺着这些字段深入它们驱动的机制：

- **u2-l2（show 规则的应用流程 `visit_show_rules`）**：看 `verdict`/`ShowStep`/`prepare` 如何协作，以及它们如何读写 `outside`（本讲 4.3 看到的「入笼/出笼」就发生在这里）。
- **u2-l6 / u2-l7（分组规则框架与生命周期）**：把本讲的 `Grouping`/`groupings` 栈推广到 `GroupingRule`/`GroupingEffect` 与 `finish_innermost_grouping` 的完整生命周期，理解 priority/effect 如何决定元素的归属。
- **u3-l3（多生命周期与 arena 内存分配）**：若你想彻底搞懂 `State<'a,'x,'y,'z>` 那四个生命期与 `Arenas`（content/styles/bump）如何延长生命期，这是专门的一讲。
- **u3-l6（递归深度限制与错误处理）**：本讲提到的 512 次分组上限与 show 规则深度检查，在那里有完整的「防循环」全景。

阅读源码时，建议把本讲的「字段职责表」与「优先级表」常备手边：每读到一个 `s.xxx` 的读写点，就回表确认它属于配置型还是工作区、属于哪档优先级，这样不容易在 `visit` 的递归里迷路。
