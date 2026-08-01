# 递归深度限制与错误处理

## 1. 本讲目标

前面几讲我们看到 `realize` 是一条**递归**流水线：`visit()` 会递归进序列、带样式元素、show 规则的输出；分组规则的 `finish_*` 又会把合成出的 `ParElem`/`ListElem` 等**重新喂回** `visit()`。只要用户写的 show 规则或文档结构「自指」，这条递归就可能**永不收敛**，把编译器拖进死循环。

本讲专门拆解 `realize` 用来对付无限递归的**三道防线**和与之配套的**错误延迟策略**：

- 理解 `engine.route.check_show_depth()` 这条「show 规则深度」防线（上限 `MAX_SHOW_RULE_DEPTH = 64`），以及它在 `visit_show_rules` 里如何随 `increase/decrease` 升降。
- 掌握 `visit_grouping_rules` 与 `finish_grouping_while` 里那个 `i > 512` 的**迭代计数器**：为什么 show 深度防线抓不住「show 规则 ↔ 分组规则」形成的**平衡环**，必须靠它兜底。
- 理解 `engine.delay()` 为什么把 show 规则里的错误**延迟**到内省循环结束后再决定是否上报，而不是立刻终止编译。

学完本讲，你应当能说清：对一个给定的「自指 show 规则」文档，到底会触发这三道防线中的哪一道、为什么是那一道。

## 2. 前置知识

本讲默认你已经读过 u2-l2（`visit_show_rules` 的应用流程）、u2-l7（分组生命周期）、u3-l1（标签与内省）和 u3-l3（多生命周期与 arena）。我们只复述关键事实：

- **show 规则的两种递归形态**：单条规则匹配自身输出（如 `show heading: it => heading[it]`）、多条规则互相套用。u2-l3 已经讲过单规则自递归靠 `RecipeIndex` + `is_guarded` 防住；本讲处理的是 `RecipeIndex` 抓不住的**跨规则 / 跨阶段**循环。
- **`visit_show_rules` 的骨架**（[lib.rs:353-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433)）：取 `verdict` → 必要时 `prepare` → 应用 `ShowStep`（`Recipe` 或 `Builtin`）→ 把替换内容重新喂回 `visit_styled`。本讲关注其中两处：把替换结果交给 `engine.delay`，以及围绕 `visit_styled` 的 `route.increase/check/decrease`。
- **分组与 sink**：分组把多个元素攒起来，收尾时合成出一个新元素（如 `ParElem`）再 `visit()` 一次（见 u2-l7、u2-l8）。这个「收尾→重访」正是 show↔grouping 循环的温床。
- **`Engine` 与 `Route`**：`Engine` 是编译引擎句柄，`engine.route` 记录「编译走到哪里了」的一条链路，用来检测循环导入和过深嵌套（见 [engine.rs:256-281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L256-L281)）。
- **内省循环（introspection loop）**：Typst 排版依赖 `query`/`counter` 等内省，而内省结果要等排版出来才知道，于是顶层会「排版→检查是否收敛→重新排版」迭代若干轮（典型 5 轮上限）。本讲第三模块会用到这个循环。

一个需要先建立的直觉：三道防线不是冗余，而是**各自覆盖一类 `RecipeIndex` 抓不住的循环**——

| 防线 | 常量 | 抓住的循环形态 |
| --- | --- | --- |
| show 规则深度 | `MAX_SHOW_RULE_DEPTH = 64` | show 规则输出在**深度方向**累积（每层都还套着上一层） |
| 分组迭代计数 | `> 512` | show 规则输出被分组规则**收尾重打包**，深度却**不累积**（平衡环） |
| 延迟错误 | `engine.delay` | （非防循环，而是错误处理策略）show 规则在某一轮内省里报错 |

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | 主战场：`visit_show_rules`（防线一、三的接入点）、`visit_grouping_rules` 与 `finish_grouping_while`（防线二）。 |
| `crates/typst-library/src/engine.rs` | `Engine::delay`、`Sink` 的延迟错误存储、`Route` 结构体及其 `increase/decrease/check_show_depth/within`。 |
| `crates/typst/src/lib.rs` | workspace 顶层：内省循环本体，以及循环结束后「提升延迟错误」的逻辑。 |
| `tests/suite/scripting/recursion.typ` | 触发 `maximum show rule depth exceeded` 的真实测试用例。 |
| `tests/suite/styling/show.typ` | 触发 `maximum grouping depth exceeded` 的真实测试用例（`#show par: box`）。 |

> 说明：本讲引用了跨 crate 的源码。永久链接一律指向当前 HEAD `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。

## 4. 核心概念与源码讲解

### 4.1 防线一：show 规则深度 `check_show_depth`

#### 4.1.1 概念说明

最直白的递归灾难是「show 规则匹配自己的输出」。比如：

```typst
#show math.equation: $x$
$ x $
```

`$ x $` 产生一个 `EquationElem`，show 规则把它替换成 `$x$`——**又是一个** `EquationElem`，于是规则再次命中，无穷无尽。注意：这里 `RecipeIndex` 的 guard（u2-l3）**救不了**我们，因为每次替换产出的是**全新的、未被 guard 的**元素副本，`is_guarded` 检查永远不成立。

这类循环的特征是：**每一层 show 规则的输出，在还没被「消化」之前，就嵌套地再次触发了 show 规则**。换句话说，递归在**调用栈深度方向**单调增长。Typst 用一个简单的计数器抓它：进入一次 show 规则应用就 `+1`，出来就 `-1`，一旦**当前深度**超过 64 就报错。

#### 4.1.2 核心流程

防线一在 `visit_show_rules` 里只有三行，但位置极其讲究：

```
进入 visit_show_rules（要对某元素套 show 规则了）
  ├─ route.increase()            # 深度 +1：标记「我又进了一层 show」
  ├─ check_show_depth()?         # 超过 64 立刻 bail，附两条 hint
  ├─ visit_styled(realized, ...) # 把 show 规则输出递归喂回去（可能再触发 show）
  └─ route.decrease()            # 深度 -1：这一层 show 处理完了
```

关键不变量是：`increase` 与 `decrease` **严格夹住** `visit_styled`。也就是说，只要 show 规则的输出在 `visit_styled` 内部**再次**命中 show 规则，那次命中又会再 `increase`，深度就**真的往上叠**。`check_show_depth` 在每次进入时校验，于是深度一旦突破 64，最内层那次 `visit_show_rules` 就会 bail，附带两条很友好的提示。

#### 4.1.3 源码精读

**接入点——夹住 `visit_styled` 的三行**：

[lib.rs:417-425](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L417-L425) —— `visit_show_rules` 在真正递归进 `visit_styled` 之前 `increase` 并校验深度，之后 `decrease`。注意第 420 行用 `.at(content.span())?` 把错误**绑定到具体元素的位置**，这样报错能指到出问题的那一行源码。

**深度上限与校验函数**：

[engine.rs:336-363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L336-L363) —— 注释点明一个精巧设计：四类深度上限（show=64、layout=72、html=72、call=80）**故意取不同值**，「越小的上限，优先级越高」。这样即便 show 规则深度和函数调用深度交织在一起，只要是 show 规则的问题，总是先撞到 64 这条线、报出更准确的 show 规则错误，而不是先撞到 80 的 call 深度。

`check_show_depth` 调 `self.within(MAX_SHOW_RULE_DEPTH)` 判定，超了就 `bail!("maximum show rule depth exceeded"; hint: ..., hint: ...)`。

**`within` 如何算「当前深度」**：

[engine.rs:404-428](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L404-L428) —— `Route` 是一条链（`outer` 指向父段），每段记录本段贡献的 `len`。`within(depth)` 把链上各段 `len` 累加，判断是否 `<= depth`。那个 `upper: AtomicUsize` 是一个**上界缓存**：一旦发现某条子链其实很浅，就用 compare-exchange 把上界压低，下次同形状的调用能更快短路，且**不破坏 comemo 的缓存复用**（注释解释：如果每次都精确计算深度，不同深度的等价计算就无法共享缓存了）。

**真实触发案例**：

[tests/suite/scripting/recursion.typ:51-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/tests/suite/scripting/recursion.typ#L51-L57) —— `#show math.equation: $x$` 配 `$ x $`，测试断言输出正是 `maximum show rule depth exceeded` 并附那两条 hint。同一文件里 `#show heading: it => heading[it]`（L59-64）走的是同一条防线。

#### 4.1.4 代码实践

**实践目标**：亲手触发防线一，确认错误信息与 hint。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 打开 `tests/suite/scripting/recursion.typ`，找到 L51-57 的 `recursion-show-math` 用例，读懂它的 `// Error` / `// Hint` 断言行。
2. 在 `visit_show_rules` 的 [lib.rs:419-420](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L419-L420) 之间临时插一行日志：
   ```rust
   s.engine.route.increase();
   eprintln!("[show-depth] elem={} ", content.elem().name()); // 临时日志
   s.engine.route.check_show_depth().at(content.span())?;
   ```
3. 如果你本地能编译 typst（`cargo build`），用 `cargo test --test suite recursion-show-math` 或直接编译一个含 `$ x $` + `#show math.equation: $x$` 的小文档运行。

**需要观察的现象**：日志里 `[show-depth] elem=equation` 会**连续刷屏**，直到第 64 次进入时 `check_show_depth` bail。

**预期结果**：编译以 `maximum show rule depth exceeded` 终止，并附带 `maybe a show rule matches its own output` 与 `maybe there are too deeply nested elements` 两条 hint。这与测试断言完全一致。

**如果无法确定运行结果**：上述错误文本与 hint 已由 `tests/suite/scripting/recursion.typ:53-55` 的断言钉死，是仓库内已验证的事实；日志刷屏的具体次数依赖本地是否去掉其他短路，属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RecipeIndex` 的 guard（u2-l3）抓不住 `#show math.equation: $x$` 这种循环？

> **答案**：`RecipeIndex` guard 是烙在**同一个元素实例**上的位标记，用于防「同一条规则在**同一个**元素上重复跑」。而 `show math.equation: $x$` 每次替换都会产出一个**全新的** `EquationElem`，新副本上没有 guard 位，所以 `is_guarded` 永远为假。这类「跨元素副本」的循环只能靠调用栈深度（防线一）来抓。

**练习 2**：把 `MAX_SHOW_RULE_DEPTH` 调成 128 会让上面的例子变成「编译成功」吗？

> **答案**：不会。它只是把循环多跑 64 轮，最终仍会撞到 128 上限并报同样的错。这个常量是「安全阈值」而非「正确性边界」——任何有限值都拦得住无限递归，只是栈消耗和耗时不同。

---

### 4.2 防线二：分组迭代计数 `> 512` 与平衡环

#### 4.2.1 概念说明

现在考虑一个**更狡猾**的循环，它恰好是防线一**抓不住**的：

```typst
#show par: box
Hello
```

逐步推演发生了什么（这就是 `tests/suite/styling/show.typ` 里 `issue-5690-oom-par-box` 用例，用来防 OOM）：

1. 文本 `Hello` 被 `TEXTUAL` 分组攒起来，无正则命中，于是**播种**出一个 `PAR` 分组（u2-l8）。
2. 收尾时 `finish_par` 把 `Hello` 打包成 `ParElem`，再 `visit(ParElem)`。
3. show 规则 `show par: box` 命中 `ParElem`，替换成 `box`（一个 `BoxElem`）。注意此时 `route` 先 `increase` 再 `decrease`，**深度在这一步就回落了**。
4. `BoxElem` 是行内元素，PAR 分组把它视为 `Trigger`（[lib.rs:1054](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1054)），于是又启动一个 PAR 分组。
5. 收尾再产出 `ParElem` → show 规则再换成 `BoxElem` → 再分组……

致命之处在于：**show 规则的输出（`BoxElem`）没有在深度方向嵌套**，它在被分组「消化」之后，show 深度就降回去了；而分组深度（`MAX_GROUP_NESTING = 3`）也始终只有一层 PAR 在反复「启动→收尾」。于是防线一的 64 根本到不了，防线二的「栈深度」也到不了——这是一个在**两个子系统之间横跳**、却**各自看都很浅**的**平衡环（equilibrium cycle）**。

源码注释把这种现象概括得非常到位（见 4.2.3）。它的解法很朴素：既然深度计抓不住，就数**「收尾分组」这件事发生的次数**，超过 512 次就认定是死循环。

#### 4.2.2 核心流程

防线二有**两个**计数器，分别守在两个会产生「收尾→重访」的地方：

```
A) visit_grouping_rules 里：打断外层分组的循环
   while 还有更外层分组需要被打断 {
       finish_innermost_grouping();   # 收尾，可能产出新内容触发新分组
       i += 1;
       if i > 512 { bail "maximum grouping depth exceeded" }
   }

B) finish_grouping_while 里：通用收尾驱动器
   while f(s) {                        # 还存在需要收尾的分组
       finish_innermost_grouping();   # 收尾，可能又生出分组
       i += 1;
       if i > 512 { bail "maximum grouping depth exceeded" }
   }
```

两者都用 `bail!("maximum grouping depth exceeded")`。区别在于触发场景：A 守的是「单个元素到来时，连续打断多层外层分组」的循环；B 守的是 `finish()`（总收尾）和 `finish_interrupted()`（样式中断收尾）里的迭代。`#show par: box` 主要在 B 这条路径上累计计数。

#### 4.2.3 源码精读

**注释把循环机制讲透了**：

[lib.rs:722-732](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L722-L732) —— `visit_grouping_rules` 里的 512 守卫。注释原文（意译）：这一分支似乎只在「show 规则与分组规则之间存在循环」时命中——show 规则产出被分组规则匹配的内容，分组规则又把内容交回 show 规则处理，如此往复；**两者必须处于平衡点**，否则要么 `maximum show rule depth`、要么 `maximum grouping depth` 会被触发。

这段注释同时点明了三道防线之间的**分工关系**：平衡环归 512 管，非平衡的（深度会累积的）归 64 管。

**通用收尾驱动器的守卫**：

[lib.rs:834-850](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L834-L850) —— `finish_grouping_while` 是 `finish()` 与 `finish_interrupted()` 共用的迭代器。注释解释：收尾一个分组可能产生新内容、新分组，「理论上能持续一阵子」，为了不让它变成死循环，用一个迭代计数兜底。注意这里的 `bail!` 用的是 `Span::detached()`（无具体位置），因为总收尾时已经没有「当前元素」可指了——所以分组深度错误的 span 不如 show 深度错误精确。

**`finish` 与 `finish_interrupted` 如何调它**：

[lib.rs:788-810](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L810) —— `finish` 把闭包「还有分组没收尾吗」交给 `finish_grouping_while`；这正是 `#show par: box` 的计数一路攀到 512 的地方（每轮 `finish_par` 产出新 `ParElem`→`BoxElem`→新 PAR 分组，闭包始终返回 true）。

[lib.rs:813-831](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L813-L831) —— `finish_interrupted` 在带样式元素的子内容前后各调一次 `finish_grouping_while`，闭包改为「是否有分组被这种元素的中断规则命中」。它同样受 512 保护。

**真实触发案例**：

[tests/suite/styling/show.typ:279-283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/tests/suite/styling/show.typ#L279-L283) —— `issue-5690-oom-par-box` 用例，断言 `#show par: box` + `Hello` 输出 `maximum grouping depth exceeded`。这个 issue 编号本身就在注释里（函数名 `issue-5690-oom-par-box`），说明它就是为了防一类 OOM 回归而存在的。

#### 4.2.4 代码实践

**实践目标**：复现 show↔grouping 平衡环，在 512 计数处观察迭代次数攀升。

**操作步骤**：

1. 准备一个最小文档 `par-box-cycle.typ`：
   ```typst
   #show par: box
   Hello
   ```
   这正是 `tests/suite/styling/show.typ:281-283` 的用例。
2. 在 `finish_grouping_while` 的计数处插日志（[lib.rs:841-848](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L841-L848)）：
   ```rust
   let mut i = 0;
   while f(s) {
       finish_innermost_grouping(s)?;
       i += 1;
       if i % 50 == 0 { eprintln!("[grouping-iter] i={i}"); } // 临时日志
       if i > 512 {
           bail!(Span::detached(), "maximum grouping depth exceeded");
       }
   }
   ```
3. （可选）在 `visit_show_rules` 的 `increase/decrease` 处也插一条日志，对比 show 深度的**峰值**与分组迭代次数的**峰值**。

**需要观察的现象**：`[grouping-iter]` 会从 50、100、150……一路涨到 500+，而 show 深度的日志峰值始终**停在 1**（每次 `increase` 后立刻 `decrease`）。这正是「平衡环」的体征：分组迭代暴涨、show 深度不涨。

**预期结果**：编译以 `maximum grouping depth exceeded` 终止（与 `show.typ:280` 断言一致）。注意它的 span 是 `detached`，所以错误位置不如 show 深度错误精确。

**如果无法确定运行结果**：错误文本由测试断言钉死；日志中 show 深度「停在 1」、分组计数「涨到 512」是基于源码控制流的推演，具体打印频率取决于你插的日志条件，属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `#show par: box` 改成 `#show par: "x"`（替换成普通文本 `"x"`），还会触发 `maximum grouping depth exceeded` 吗？为什么？

> **答案**：会，原理相同。`"x"` 是一个 `TextElem`，PAR 分组同样把它视为 `Trigger`（[lib.rs:1049](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1049)），于是又被打包成新 `ParElem`，show 规则再次命中——平衡环依旧成立。关键是「show 规则的输出是 PAR 的 Trigger 元素」。

**练习 2**：为什么这道防线用「迭代次数 > 512」而不是「分组嵌套深度 > N」？

> **答案**：因为平衡环里分组**嵌套深度始终是 1**（一层 PAR 反复启动/收尾），按深度计永远抓不到。能增长的是「收尾动作发生的次数」，所以必须数次数而非深度。512 是一个足够大、不会误伤正常文档（再复杂的合法文档也凑不出几百次连续收尾）、又能及时止损的阈值。

---

### 4.3 防线之外：`engine.delay` 为何延迟 show 规则错误

#### 4.3.1 概念说明

前两道防线处理的是「停不下来的循环」。但 show 规则还有一类**非致命**的失败：规则函数本身在**某一轮内省里**报了错，可这个错可能是「假警报」。

典型场景：一个 show 规则里调用了 `query(...)` 去查文档里某些元素。在内省循环的**第 1 轮**，相关元素可能还没排版出来，`query` 返回空，规则函数因此 panic 或报错；但到了**第 2 轮**，元素出现了，规则就能正常产出。如果第 1 轮的错误立刻终止整个编译，用户就会看到一个其实并不存在的错误。

所以 Typst 的策略是：**show 规则里的错误不立刻致命**。出错时，把错误**暂存**到一个「延迟错误」队列，把这条 show 规则的输出当作**空内容**继续往下走；等整个内省循环结束后，**如果错误仍然存在**（说明它在收敛后的最终文档里依然出错），才把它提升为致命错误上报。

这个机制的核心入口就是 `engine.delay(result)`。

#### 4.3.2 核心流程

```
visit_show_rules 应用 ShowStep
  └─ result = recipe.apply(...) 或 rule.apply(...)   # 可能 Err
  └─ output = engine.delay(result)                    # 出错则：错误进 sink.delayed，output 变空
                                                       #         没错则：output = result 的值
  └─ 继续用 output（可能是空内容）走完流水线

……内省循环每轮都用一个全新的 subsink 跑一遍 ……
  └─ 只有「收敛那一轮」的 subsink 被合并进主 sink

循环结束后
  └─ delayed = sink.delayed()                          # 取出幸存的延迟错误
  └─ if !delayed.is_empty() { return Err(delayed) }    # 仍在 → 提升为致命
```

两个关键设计：

- **出错即空内容**：`delay` 在 `Err` 分支返回 `T::default()`（对 `Content` 就是空），保证流水线**不会因为一个 show 规则报错就中断**，后续元素照常处理。
- **按轮隔离**：每轮内省用独立 `subsink`，只有收敛轮的错误会被保留——这就是注释里「忽略只发生在更早迭代的错误」的实现方式。

#### 4.3.3 源码精读

**`delay` 的实现**：

[engine.rs:38-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L38-L50) —— 文档注释一语道破：「处理一个结果而不立即终止执行；它产生一个延迟错误，**只有当它存活到内省循环结束时**才会被提升为致命错误」。`Ok` 原样返回值；`Err` 把错误塞进 `sink.delayed_errors` 并返回 `T::default()`。

**`visit_show_rules` 里的接入点与注释**：

[lib.rs:396-402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L396-L402) —— 这是整段最值得读的注释：show 规则里的错误不立即终止编译，而是「用空内容继续，并把所有错误攒到一起，**如果它们活到内省循环结束**再一并展示」。理由有二：一是能忽略只发生在更早迭代的瞬时错误，二是能一次展示更多有用错误。`output = Cow::Owned(s.engine.delay(result))` 这一行就是策略落地点。

**延迟错误存在哪**：

[engine.rs:211-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L211-L219) —— `Sink` 上的 `delayed_error`（单条）与 `delayed_errors`（多条），都往 `self.delayed` 这个 `EcoVec` 里追加。

[engine.rs:183-186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L183-L186) —— `delayed()` 用 `std::mem::take` 把队列**清空并取出**，这正是循环结束后「一次性结账」的取法。

**内省循环如何隔离与提升**：

[typst/src/lib.rs:136-161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L136-L161) —— 每轮 `loop` 里都 `let mut subsink = Sink::new()`（独立队列），用收敛校验 `constraint.validate(...)` 判停；**只有收敛或放弃的那一轮**，才 `sink.extend_from_sink(subsink)` 把它的延迟错误并入主 sink。这意味着：非收敛轮里产生的瞬时错误，随着那一轮 subsink 被丢弃而**自动消失**。

[typst/src/lib.rs:187-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L191) —— 循环结束后 `let delayed = sink.delayed()`，非空则 `return Err(delayed)`。这就是「活到最后的错误才致命」的最终判决。

把这三处连起来读，`engine.delay` 的完整生命周期就清晰了：**产生（realize）→ 按轮隔离（内省循环）→ 末轮结账（提升）**。

#### 4.3.4 代码实践

**实践目标**：验证「早期迭代的延迟错误会被丢弃，只有持续到收敛轮的错误才上报」。

**操作步骤**（源码阅读型）：

1. 在 [lib.rs:402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L402) 的 `engine.delay` 调用处，临时改成区分日志：
   ```rust
   let result = /* 原来的 match step {...} */;
   match &result {
       Ok(_) => {}
       Err(e) => eprintln!("[show-delay] show rule errored in some iter: {}", e[0].message),
   }
   output = Cow::Owned(s.engine.delay(result));
   ```
2. 构造一个会在首轮失败、后续轮次成功的 show 规则。一个可参考的思路：写一个依赖 `query(<lbl>)` 的 show 规则，使得首轮（query 为空）走异常分支、后续轮次（query 有值）正常。具体文档需依本地 Typst 版本调试。
3. 同时在 [typst/src/lib.rs:188-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L188-L191) 的提升处加日志，打印最终 `delayed` 的长度。

**需要观察的现象**：`[show-delay]` 可能在前几轮内省里打印（首轮 query 为空触发的瞬时错误），但只要文档最终收敛、且收敛轮里该 show 规则不再报错，最终 `delayed` 长度就是 **0**，编译成功——早期错误被「忽略」了。

**预期结果**：若文档能收敛且收敛轮无错，编译成功、无致命错误；若收敛轮里该 show 规则**仍然**报错，则 `delayed` 非空，编译以该错误终止。

**如果无法确定运行结果**：`engine.delay` 的「出错误即返回默认值」与「按轮隔离 subsink」是源码确定的行为；但具体「哪个 show 规则会在首轮失败而后续成功」依赖你构造的文档与内省时序，属「待本地验证」。读源码本身已能确认机制：非收敛轮的 subsink 不会被合并。

#### 4.3.5 小练习与答案

**练习 1**：`engine.delay` 和防线一、防线二是什么关系？它防循环吗？

> **答案**：不防循环。防线一、二解决「停不下来」（递归无界），`engine.delay` 解决「**报错时机**」（错误该不该现在就致命）。它让 show 规则在某一轮里的失败不立即终止编译，配合内省循环的按轮隔离，过滤掉「瞬时假警报」。三者正交：一个 show 规则可能既不触发循环、又恰好会在首轮报错，这时靠 `delay` 暂存；若它真的永不收敛，则可能同时撞上循环防线。

**练习 2**：为什么 `delay` 在 `Err` 分支返回 `T::default()`（空内容），而不是直接返回一个「错误占位元素」？

> **答案**：返回空内容能让流水线**对下游透明地继续**——后续分组、过滤、收尾都不受这条 show 规则失败的影响，最大程度保住「其余内容照常产出」。如果塞一个占位元素，反而可能干扰分组判定（比如被当成 Trigger），引入新的、与原问题无关的怪现象。空内容是语义最中性的「降级」。

---

## 5. 综合实践

把三道防线串起来做一次「分类诊断」练习。准备下面三个文档，**逐一预测**它们各自会触发哪道防线（或哪条错误），再读源码/跑测试验证：

| 文档 | 你的预测 | 验证依据 |
| --- | --- | --- |
| `#show math.equation: $x$` + `$ x $` | ? | `recursion.typ:51-57` |
| `#show par: box` + `Hello` | ? | `show.typ:279-283` |
| 一个依赖 `query` 的 show 规则，首轮失败 | ? | `engine.rs:38-50` + `typst/src/lib.rs:187-191` |

要求：

1. 对每个文档，画出它的「递归/报错路径」：show 规则在哪一步被应用、深度是否累积、是否经过分组收尾重打包、错误是否被 `delay`。
2. 解释**为什么**防线一抓得住第一个却抓不住第二个——关键词：深度是否单调累积、收尾重打包。
3. 在 `visit_show_rules` 与 `finish_grouping_while` 各插一条计数日志，实际运行（若本地可编译），把「show 深度峰值」与「分组迭代峰值」填进下表，与自己画的路径对照：

   | 文档 | show 深度峰值 | 分组迭代峰值 | 实际错误 |
   | --- | --- | --- | --- |
   | equation 循环 | ? | ? | ? |
   | par↔box 循环 | ? | ? | ? |

完成此练习后，你应该能对任意一个「行为诡异的自指 show 规则」快速定位它落在三道防线的哪一格。

## 6. 本讲小结

- `realize` 用**三道互补防线**对付 `RecipeIndex` 抓不住的循环：show 规则深度（64）、分组迭代计数（512）、错误延迟（`engine.delay`，非防循环）。
- **防线一**：`visit_show_rules` 用 `route.increase/check_show_depth/decrease` 夹住 `visit_styled`，抓「show 输出在深度方向累积」的循环（如 `#show math.equation: $x$`）；四类深度上限故意递增，保证 show 问题优先被准确报出。
- **防线二**：当 show 规则产出被分组规则**收尾重打包**、深度却**不累积**时（如 `#show par: box`），形成「平衡环」，64 和分组嵌套深度都抓不到，只能靠 `visit_grouping_rules` 与 `finish_grouping_while` 里的 `> 512` 迭代计数器兜底。
- **`engine.delay`**：show 规则的错误不立即致命——出错时错误进 `sink.delayed`、输出退化为空内容，流水线继续；配合内省循环「按轮隔离 subsink，仅合并收敛轮」，能丢弃只在早期迭代出现的瞬时假警报，只在错误存活到循环结束时才提升为致命。
- 三者的**分工**一句话：平衡的循环归 512，非平衡（深度累积）的归 64，报错时机归 `delay`——它们正交、各司其职。
- 源码注释（尤其 [lib.rs:396-402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L396-L402) 与 [lib.rs:725-730](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L725-L730)）本身就把这套分工讲得很清楚，值得反复读。

## 7. 下一步学习建议

本讲是 `typst-realize` 专家单元（u3）的收官篇，realize 内部的机制至此基本讲完。建议：

- **向上看调用方**：结合 u3-l5（与 layout/html/bundle/math 的集成），观察下游 crate 在拿到 `realize` 的 `Vec<Pair>` 后，如何把这里的「延迟错误」与「循环防线」纳入各自的错误处理；尤其 `typst-bundle` 直接用了 `engine.sink.delayed_error(...)`（见 u3-l5 提到的 `typst-bundle/src/lib.rs:269`、`308`），可与本讲的 `delay` 对照。
- **横向看内省**：`engine.delay` 的价值完全建立在「内省循环」之上，建议去读 `crates/typst-library/src/introspection/` 下的 `convergence.rs` 与 `Introspect` trait，理解「为什么不收敛时要跑 5 轮」「`analyze` 如何诊断不收敛」，从而彻底理解延迟错误的去留时机。
- **动手扩展**：若你想为某个自定义元素加一条「可能自指」的内置 show 规则，回到 u2-l2/u2-l3 复习 `RecipeIndex` 与 `NativeShowRule`，再对照本讲确认你的规则会被哪道防线保护——这是检验你是否真正理解三道防线的最好方式。
