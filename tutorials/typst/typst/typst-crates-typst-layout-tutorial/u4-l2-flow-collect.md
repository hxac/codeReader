# flow 收集：Collector 与 Child 类型

## 1. 本讲目标

上一讲（u4-l1）我们看清了 `layout_flow` 的三段式骨架：`configuration` → `collect` → 主循环（每区域一次 `compose`）。本讲钻进三段式的**第二步**——`collect`，回答这几个问题：

1. realize 产出的 `Vec<Pair>` 是什么样的数据？为什么不能直接拿去排版？
2. `collect` 把 `Pair` 翻译成了哪几种 `Child`？每种 `Child` 封装了什么？
3. 段落为什么在收集阶段就断好了行，而块却要把排版推迟到 `compose`？
4. `Child::Rel(_, u8)`、`Child::Fr(_, u8)` 里那个 `u8` 是什么？它如何决定相邻间距谁吃掉谁？
5. `place` 元素的「作用域」（column / parent）是什么，浮动体又为何特殊？

学完后你应当能：拿着一段 Typst 源码，画出它经过 `collect` 后产出的 `Child` 序列；并说清 `par_situation` 在序列中如何随元素流转。

## 2. 前置知识

本讲建立在 u4-l1 已建立的概念之上，回顾三个关键点：

- **Pair**：realize 的产物。它只是一个类型别名，即「内容指针 + 作用于它的样式链」的二元组：

  [`crates/typst-library/src/routines.rs:195-196`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L195-L196) —— `pub type Pair<'a> = (&'a Content, StyleChain<'a>);`

  `Content` 是运行时多态的「任意元素」，要靠 `to_packed::<T>()` 才能知道它具体是段落、块、间距还是别的。也就是说 `Vec<Pair>` 是**异构、带多态、未解析样式**的原始清单。

- **layout_flow 主循环每个区域跑一次 `compose`**（见 u4-l1）。如果每次都重新判断元素类型、重新解析样式，等于把同样的事重复 N 遍（N = 区域数）。

- **Regions 的宽度跨区域不变，只有高度在变**（见 u2-l2）。这条性质是本讲最关键的「直觉钩子」——它决定了哪些工作能提前做、哪些必须推迟。

- **Locator / Tag**（u2-l4）：每个排版实例需要一个稳定位置身份，隐形 tag 要随内容流动。`collect` 也要负责给每个子元素分配 locator。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/flow/collect.rs` | 本讲主角。定义 `collect` 入口、`Collector` 状态机、`Child` 枚举及全部子类型（`LineChild`/`SingleChild`/`MultiChild`/`MultiSpill`/`PlacedChild`/`CachedCell`）。 |
| `src/flow/mod.rs` | 调用方。`layout_flow` 在主循环前调用 `collect`；并定义 `FlowMode`、`Work`（消费 `Child` 的跨区域状态）。 |
| `src/flow/distribute.rs` | 消费方之一。`Distributor` 把每个 `Child` 放进当前区域，其中 `keep_weak_rel_spacing` 读取 `Child` 携带的 weakness 等级做间距折叠。 |
| `src/inline/mod.rs` | `collect` 在收集段落时会回调 `crate::inline::layout_par` 直接断行；`ParSituation` 枚举也定义在此。 |

## 4. 核心概念与源码讲解

### 4.1 为什么要「预处理」：collect 的职责与动机

#### 4.1.1 概念说明

把 `Vec<Pair>` 想象成一袋「没拆封的快递」：每件包裹外表都一样（都是 `Content`），你得拆开才知道里面是段落、是块、是一段垂直间距，还是个浮动体；而且每件包裹上挂着的样式链（`StyleChain`）还要现场解析成具体的数值（间距多少、是否弱、是否可断裂、对齐方式……）。

`collect` 就是**一次性拆包、分类、贴标签**的环节：它把异构的 `Pair` 翻译成结构化的 `Child` 枚举——一种类型对应一个变体，标签上写好后续排版要用到的全部数值。这样下游 `compose`/`distribute` 每个区域只需「读标签、摆东西」，再也不用重复拆包。

更重要的是，`collect` 还会**提前做完所有「与具体区域无关」的工作**：

- 解析样式（间距、对齐、weak、breakable、scope 等都是区域无关的）；
- 给每个子元素分配 locator（位置身份）；
- **给段落断行**（line-breaking 只依赖宽度，而宽度跨区域不变，所以能提前做）；
- 计算 widow/orphan 需要预留的高度。

而「与区域相关」的工作（一个块在多高的情况下怎么断、能放几行）则**故意推迟**到 `compose`，因为这部分依赖当前 region 的剩余高度与 backlog。

> 一句话直觉：**collect 做完所有「宽度相关、与高度无关」的事；高度相关的判断留给每区域的 compose。**

#### 4.1.2 核心流程

`collect` 的调用点在 `layout_flow` 里，紧跟 `configuration` 之后：

1. `layout_flow` 先算出 `Config`（含列宽），再建一个 `Bump`（bump 分配器，见 4.3）。
2. 调 `collect(engine, &bump, children, locator, base, expand, mode)`，其中 `base = Size::new(config.columns.width, regions.full)`——**列宽**作为段落断行的依据。
3. `collect` 内部构造 `Collector` 并 `.run(mode)`，按 `FlowMode` 分流到 `run_block` 或 `run_inline`。
4. 产出的 `Vec<Child>` 被 `Work::new(&children)` 持有，进入主循环。

#### 4.1.3 源码精读

`collect` 本身是个薄入口，真正干活的是 `Collector`：

[`src/flow/collect.rs:32-52`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L32-L52) —— 构造 `Collector`（注意 `par_situation` 初始化为 `First`），调用 `.run(mode)`。

调用方在 `layout_flow` 里如何喂数据：

[`src/flow/mod.rs:208-217`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L208-L217) —— `collect(...)` 的调用，`base` 取 `Size::new(config.columns.width, regions.full)`，`expand` 取 `regions.expand.x`（只关心水平 expand）。

注意 `collect` 带了 `#[typst_macros::time]`（[L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L31)），会被计入 Typst 的性能计时面板——说明这一步足够重（要给所有段落断行），值得单独观测。

#### 4.1.4 代码实践

**实践目标**：确认「段落断行发生在 collect、块排版发生在 compose」这条切分。

**操作步骤**：

1. 打开 `src/flow/collect.rs` 的 `par` 方法（[L158-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L158-L185)），看到它在收集阶段就调用 `crate::inline::layout_par` 并 `.into_frames()` 拿到若干行 frame。
2. 再打开 `block` 方法（[L234-279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L234-L279)），对比发现它**没有**调用任何 layout 函数，只是把 `&Packed<BlockElem>` 连同解析好的样式塞进 `SingleChild`/`MultiChild`，真正的 `layout_single_block`/`layout_multi_block` 调用要等到 `SingleChild::layout`（[L391-409](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L391-L409)）在 distribute 时才发生。

**需要观察的现象 / 预期结果**：段落一进入 collect 就被「拍扁」成一串 `Child::Line`；而块在 collect 阶段只是「打包好等用」。这是切分的直接证据。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 layouter 既依赖宽度又依赖高度，它应该在 collect 里做还是 compose 里做？为什么？

**参考答案**：在 compose 里做。collect 只保留「与区域（尤其高度）无关」的工作，是为了避免每个区域重复计算；一旦依赖高度，就只能像块那样封装成 `Child` 推迟到每区域的 distribute。

**练习 2**：为什么 `collect` 收到的 `base` 用的是 `regions.full`（整区域高度）而不是 `regions.size.y`（当前剩余高度）？

**参考答案**：因为段落断行只看宽度；高度分量只是顺带塞进 `Size` 给 `layout_par` 当占位，用 `full`（稳定、代表「一行不限制高度」的语义）比用会被逐区域削短的 `size.y` 更合适，也保证断行结果跨区域一致。

---

### 4.2 Collector 的运行骨架：类型分派与 par_situation

#### 4.2.1 概念说明

`Collector` 是个一次性的状态机：持有 `engine`、`bump`、输入 `children`、`locator`（已 `split`）、`base`、`expand`，以及两个会随遍历更新的字段——`output: Vec<Child>`（累积产物）和 `par_situation: ParSituation`（段落情境）。

`ParSituation` 是个三态枚举，影响首行缩进是否生效：

[`src/inline/mod.rs:249-258`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L249-L258) —— `First`（流的首个孩子或列断后）/`Consecutive`（紧跟另一个段落）/`Other`（其它，比如跟在块后面）。

`run(mode)` 按 `FlowMode` 分流：`Root | Block` 走 `run_block`（块级流），`Inline` 走 `run_inline`（行内流）。

#### 4.2.2 核心流程

`run_block` 是一长串 `if let Some(elem) = child.to_packed::<T>()` 的类型分派，每种 Typst 元素映射到一个处理方法：

```
TagElem      → Child::Tag(&tag)
VElem        → v()            （垂直间距 → Child::Rel / Child::Fr）
ParElem      → par()          （段落断行 → 一串 Child::Line）
BlockElem    → block()        （块 → Child::Single 或 Child::Multi）
PlaceElem    → place()        （定位元素 → Child::Placed）
FlushElem    → Child::Flush   （清理浮动）
ColbreakElem → Child::Break(weak)
PagebreakElem → bail!         （容器内禁止分页，报错并提示用 colbreak）
其它          → engine.sink.warn(...)   （分页导出时忽略，发警告）
```

`par_situation` 的流转规则（这是本讲实践的重点）：

- 初始：`First`（collect 构造时设）。
- 遇到 `ParElem`：处理完置为 `Consecutive`（表示「下一段若也是段落，则属于连续段落」）。
- 遇到 `BlockElem`：置为 `Other`。
- 遇到 `ColbreakElem`：重置为 `First`（新列的首段又算首段）。

`run_inline` 走另一条路：先用 `split_prefix_suffix` 把首尾的 `TagElem` 切出来，对中间内容调用 `crate::inline::layout_inline` 排成行，再用 `lines` 收集，最后把首尾 tag 分别 push 进 output。

#### 4.2.3 源码精读

`run_block` 的类型分派长链：

[`src/flow/collect.rs:76-108`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L76-L108) —— 逐个 `Pair` 用 `to_packed::<T>()` 判断类型并分派；注意 `ColbreakElem` 分支里 `self.par_situation = ParSituation::First`。

`run_inline` 的首尾 tag 切分：

[`src/flow/collect.rs:111-144`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L111-L144) —— `split_prefix_suffix` 切出首尾 tag，`StyleChain::trunk_from_pairs` 取公共样式，再 `layout_inline` 排版。

`par_situation` 在 `block` 末尾被置为 `Other`：

[`src/flow/collect.rs:278`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L278) —— `self.par_situation = ParSituation::Other;`。

#### 4.2.4 代码实践

**实践目标**：手工追踪 `par_situation` 在一段混合内容里的流转。

**操作步骤**：假定 `children` 依次为 `[Par, Block, Par, Colbreak, Par]`，按 `run_block` 的规则逐步写出每处理完一个元素后 `par_situation` 的值，以及每个 `Par` 收到的情境。

**预期结果**：

| 处理的元素 | 处理前 situation | 该 Par 收到 | 处理后 situation |
| --- | --- | --- | --- |
| `Par` | First | First | Consecutive |
| `Block` | Consecutive | — | Other |
| `Par` | Other | Other | Consecutive |
| `Colbreak` | Consecutive | — | First |
| `Par` | First | First | Consecutive |

含义：第 1、5 段落在「首段」位置（可能不缩进），第 3 段跟在块后属 `Other`。

> 若想本地验证：可在 `par` 方法里临时加 `eprintln!("par situation={:?} span={:?}", self.par_situation, elem.span());`，编译运行 `cargo test`（或用 `cargo run` 编译一个含上述结构的 `.typ`）观察输出。改完记得还原，不要提交。

#### 4.2.5 小练习与答案

**练习 1**：为什么容器内的 `PagebreakElem` 要直接 `bail!` 而不是忽略？

**参考答案**：因为片段级排版（容器/块）没有「页」的概念，分页符在这里无意义；静默忽略会让用户困惑为何没换页，所以报错并提示改用 `#colbreak()`（见 [L93-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L93-L97)）。

**练习 2**：`run_inline` 为什么要单独把首尾 tag 切出来，而不是和中间内容一起排？

**参考答案**：tag 是隐形的 `FrameItem`，不参与行内排版（不该被断行/整形）；但它需要按顺序挂在结果的前后，所以先切出、排完中间再分别 push（[L130-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L130-L141)）。

---

### 4.3 Child 类型全景与 weakness 分级

#### 4.3.1 概念说明

`Child` 是 collect 的最终产物，一个 9 变体枚举。注释里有一句关键设计说明：「The larger variants are bump-boxed to keep the enum size down」——大变体（`Line`/`Single`/`Multi`/`Placed`）被装进 bump 分配的 `BumpBox`，避免枚举本体过大（Rust 枚举的大小等于最大变体，不装箱会让每个 `Child` 都占最大那份内存）。

`bumpalo::Bump` 是一个**区域性分配器（arena）**：一次性分配、整体释放，比逐个 `Box` 更快。`layout_flow` 里 `let bump = Bump::new();`（[mod.rs:208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L208)）创建，作用域结束自动回收。`Collector::boxed` 把值包成 `BumpBox`（[L338-340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L338-L340)）。

`Child::Rel(Rel<Abs>, u8)` 与 `Child::Fr(Fr, u8)` 的第二个字段是 **weakness（弱度）等级**，一个 `u8`。规则：**数值越小越「强」**；`0` 是强间距（不可丢弃），`>=1` 是弱间距（在断点/相邻时会被折叠或裁剪）。

#### 4.3.2 核心流程

各来源给间距打的 weakness 等级（越小越强）：

| 来源 | 变体 | weakness | 含义 |
| --- | --- | --- | --- |
| `VElem`（手动垂直间距），`weak=false` | `Rel`/`Fr` | 0 | 强间距，必留 |
| `VElem`，`weak=true` | `Rel`/`Fr` | 1 | 弱间距，可丢 |
| `BlockElem` 的 `above/below` 为 `Fr(..)` | `Fr` | 2 | 块的分数间距 |
| `BlockElem` 的 `above/below` 为 `Custom(Rel)` | `Rel` | 3 | 块的自定义相对间距 |
| `BlockElem` 的 `above/below` 为 `Auto`（回落到 `par.spacing`）；`ParElem` 的段间距 | `Rel` | 4 | 自动/段落间距 |
| 段落内行间 `leading` | `Rel` | 5 | 行距（最弱） |

这套分级让下游 `Distributor` 能在断点处自动判断「相邻几个间距谁该吃掉谁」。例如块的自定义间距（3）强于段落间距（4），所以块紧贴段落时块间距胜出；而行距（5）最弱，断行处会被裁掉。

消费方在 `distribute.rs` 的 `keep_weak_rel_spacing` / `keep_weak_fr_spacing`：它们从 items 末尾向前扫，遇到「弱度不大于自己的弱间距」就用二者最大值覆盖、丢弃新项；遇到强间距（`Abs(_, 0)`）或 tag/Placed 则「跳过继续看」；遇到 Frame 则「撑得住、保留」。

#### 4.3.3 源码精读

`Child` 枚举定义：

[`src/flow/collect.rs:343-366`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L343-L366) —— 9 个变体；注释说明大变体被 bump-boxed 以缩小枚举。

间距 weakness 的赋值点（`v` 方法）：

[`src/flow/collect.rs:147-154`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L147-L154) —— `VElem` 的 `weak` 转 `u8`（真→1，假→0）。

块间距的 weakness 赋值（`block` 内的 `spacing` 闭包）：

[`src/flow/collect.rs:245-250`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L245-L250) —— `Auto→4`、`Custom(Rel)→3`、`Custom(Fr)→2`。

消费方如何用 weakness：

[`src/flow/distribute.rs:203-228`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L203-L228) —— `keep_weak_rel_spacing` 的折叠逻辑（弱度小的覆盖弱度大的、同弱度取较大值）。

#### 4.3.4 代码实践

**实践目标**：理解「弱度小的吃掉弱度大的」。

**操作步骤**：读 `distribute.rs` 的 `keep_weak_rel_spacing`（[L203-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L203-L228)）。假设 items 末尾依次是 `Abs(10pt, 4)`（段落间距）后面紧跟一个 `Child::Rel(8pt, 3)`（块的自定义间距）。

**需要观察的现象 / 预期结果**：新来的 8pt@3 弱度更小（更强），条件 `weakness(3) <= prev_weakness(4)` 成立且 `3 < 4`，于是旧项被改写为 `Abs(8pt, 3)`，并回退高度差 `8-10=-2pt`（即丢弃了原来 10pt 中的 2pt，整体只保留更强的 8pt）。结论：相邻时强间距覆盖弱间距，而不是简单相加。

#### 4.3.5 小练习与答案

**练习 1**：行距 `leading` 的 weakness 是 5（最弱），它在什么情况下会被裁掉？

**参考答案**：当某行恰好落在区域末尾、成为断点时，它后面的 leading 属于「尾随弱间距」，会被 `trim_spacing`/断点逻辑裁掉，避免在区域底部留一段空白行距（见 distribute.rs 的 `trim_spacing` [L261-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L261-L277)）。

**练习 2**：为什么 `Child::Tag` 和 `Child::Placed` 在 `keep_weak_rel_spacing` 里被「peeked beyond」（跳过继续往前看）？

**参考答案**：tag 是隐形的不占位，placed 是绝对/浮动定位不参与正常流间距挤压；它们不该阻断弱间距折叠的扫描，所以要跳过它们继续看前面有没有可合并的弱间距（[distribute.rs:219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L219)、[249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L249)）。

---

### 4.4 块的两种封装：SingleChild / MultiChild / MultiSpill

#### 4.4.1 概念说明

`BlockElem` 经过 `block()` 后会被装进两种封装之一：

- **`SingleChild`**：**不可断裂**的块（`breakable=false`），或高度为 `Fr`（分数高度）的块。它排版后只产**单帧** `Frame`。
- **`MultiChild`**：**可断裂**的块。它排版后产 `Fragment`（多帧序列），跨区域时把「排不完的剩余」装进 **`MultiSpill`** 在区域间接力。

为什么要分两种？因为可断裂块要在区域间拆分（可能第一页放前半段、第二页放后半段），需要一种「跨区域保存剩余待排内容」的机制——这就是 `MultiSpill`。而不可断裂块要么整个放进当前区域、要么整个推到下一区域，不需要 spill。

> 关联 u4-l1 的 `Work` 结构：`Work.spill: Option<MultiSpill>` 就是跨区域接力可断裂块剩余内容的字段（[mod.rs:305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L305)）。

#### 4.4.2 核心流程

`block()` 的判定与封装：

1. 读 `breakable`、`height`（判断是否 `Fr`）、`sticky`、`alone`（`children.len() == 1`）。
2. 在块前后各 push 一个 spacing（`above`/`below`）。
3. **`if !breakable || fr.is_some()` → `Child::Single`；否则 `Child::Multi`**。
4. 块处理完置 `par_situation = Other`。

`MultiChild::layout` 的「取首帧 + 剩余 spill」逻辑：

1. 调 `layout_full` 拿整个 `Fragment`。
2. 取第一帧返回。
3. 若还有更多帧 → 把剩余信息打包成 `MultiSpill`（记录 `full`、首区域高度 `first`、`min_backlog_len` 等）返回。

`MultiSpill::layout` 的接力：

1. 把当前 region 的高度 `push` 进自己的 `backlog`（已提交、不可变）。
2. 合并 `self.backlog` 与新 region 的 backlog，裁掉末尾与 `last` 重复的项（防止 backlog 无谓增长、改变哈希）。
3. 用合并后的 regions 再次 `layout_full`，然后 `skip(self.backlog.len())` 跳过已处理帧，取下一帧。

**重要设计**：`expand.y &= self.alone`——只有当块是流的**唯一孩子**时，才保留纵向撑满（`expand.y`）。否则一个块不应该独占整个容器高度。

#### 4.4.3 源码精读

`block()` 的分流：

[`src/flow/collect.rs:254-275`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L254-L275) —— `!breakable || fr.is_some()` 走 `SingleChild`，否则 `MultiChild`；二者都带 `cell: CachedCell::new()`。

`MultiChild::layout` 取首帧并产 spill：

[`src/flow/collect.rs:457-483`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L457-L483) —— `frames.next()` 取首帧；若 `frames.next().is_some()` 还有剩余则构造 `MultiSpill`。

`MultiSpill::layout` 的 region 拼接与 skip：

[`src/flow/collect.rs:556-612`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L556-L612) —— 注意 [L592-600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L592-L600) 的注释坦诚承认：这套 region 拼接是「旧模型（一次性给所有 region）与新模型（按需逐区域）」之间的兼容层，并非 100% 正确（旧模型里后续 region 本可能影响前面帧），是多 layouter 重构前的过渡方案。

`expand.y &= self.alone`：

[`src/flow/collect.rs:492-494`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L492-L494) —— `MultiChild::layout_full` 里改写 region 的纵向 expand。

#### 4.4.4 代码实践

**实践目标**：看清可断裂块如何跨两个区域拆分。

**操作步骤**：读 `MultiChild::layout`（[L457-483](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L457-L483)）与 `MultiSpill::layout`（[L556-612](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L556-L612)）。假设一个可断裂块 `B` 在 region1（高度 h1）排版得到 3 帧 `[f1, f2, f3]`。

**需要观察的现象 / 预期结果**：

- 第一轮 `MultiChild::layout`：取 `f1` 返回，因还有 `f2/f3` → 返回 `spill = Some(MultiSpill { first: h1, backlog: [], min_backlog_len: ... })`。
- region2（高度 h2）调用 `spill.layout`：先把 h1 push 进 backlog，合并得到 `backlog=[h1]` + region2 的后续，再用 `Regions{ size: (w, h1), backlog: [h1, ...] }` 重新 `layout_full`，`skip(self.backlog.len()=1)` 跳过 f1，取到 f2。
- 若仍有 f3，再返回一个 spill，依此类推直到排完。

> 注意 `MultiSpill` 的 `min_backlog_len` 单调不增地保护（[L600](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L600)），让 `unwrap` 拿下一帧相对安全。

#### 4.4.5 小练习与答案

**练习 1**：为什么高度为 `Fr` 的块（`fr.is_some()`）即便 `breakable=true` 也被装进 `SingleChild`？

**参考答案**：`Fr` 高度意味着块要「按比例瓜分剩余空间」，这种块是作为一个整体参与空间分配的（它自身的尺寸由 fr 分配决定，而不是被高度限制拆分），所以按不可断裂处理（[L254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L254)）。它的 `fr` 字段会被 `SingleChild` 带着传给下游参与 fr 分配。

**练习 2**：`MultiSpill::layout` 为什么要 `skip(self.backlog.len())` 而不是直接取第一帧？

**参考答案**：每次 `layout_full` 都是用「累积的所有已提交 region」重新排版，返回的 Fragment 开头那些帧对应的是**已经处理并提交过**的区域；只有跳过它们，才能拿到「当前这个新区域」对应的那一帧。

---

### 4.5 PlacedChild 与 placement 作用域

#### 4.5.1 概念说明

`PlaceElem`（对应 Typst 的 `place` 函数）把内容从正常流里「拎出来」绝对/浮动定位。`place()` 方法先做一组**合法性校验**，再把解析好的参数封装成 `PlacedChild`。

**作用域 `scope`** 来自 `PlacementScope` 枚举，只有两个变体：

[`crates/typst-library/src/layout/place.rs:184-190`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L184-L190) —— `Column`（定位到当前列，默认）/ `Parent`（相对父级、横跨所有列）。

> 注意：枚举里**没有** `Page` 变体。本讲规格里提到的「page」作用域，指的是 `Parent` 在运行时的效果——`Parent` 作用域的浮动体在 `compose.rs` 里被当作**页级插入**（`page_insertions`，横跨所有列），而 `Column` 作用域则是**列级插入**（`column_insertions`）。即「page/column」是 compose 阶段的插入层级，对应到 collect 阶段就是 `PlacementScope::Parent/Column`。

**`float`（浮动）**是另一维开关。浮动体参与「挤占正文区域 + 触发重排」的复杂流程（见 u4-l4）。`place()` 对 `float` 与 `align_y`/`scope` 的组合做了严格校验。

#### 4.5.2 核心流程

`place()` 的校验与封装：

1. 解析 `alignment`、`scope`、`float`、`clearance`、`dx`/`dy`（位移 delta）。
2. 校验组合：
   - 浮动 + 垂直对齐不是 `auto`/`top`/`bottom`（如 `center`）→ 报错。
   - 非浮动 + `align_y` 为 `Auto` → 报错（自动定位只对浮动开放）。
   - 非浮动 + `scope == Parent` → 报错（parent 作用域目前只对浮动开放）。
3. 分配 locator，封装成 `Child::Placed(PlacedChild { ... })`。

`PlacedChild::layout` 的排版：

1. 用 `place` 的对齐覆盖 `AlignElem::alignment`，链上样式。
2. 调 `crate::layout_frame` 排 body（单区域，expand 全关）。
3. 若是浮动体 → `frame.set_parent(FrameParent::new(location, Inherit::Yes))`，把整帧挂到一个 parent group 下（关联 u2-l3 的 FrameParent / u2-l4 的内省顺序修正）。

#### 4.5.3 源码精读

`place()` 的校验长链：

[`src/flow/collect.rs:282-314`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L282-L314) —— 三组 `bail!` 校验浮动/对齐/作用域组合。

封装 `PlacedChild`：

[`src/flow/collect.rs:316-331`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L316-L331) —— 解析 `clearance`、`delta`，构造 `Child::Placed`。

`PlacedChild::layout`：

[`src/flow/collect.rs:638-663`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L638-L663) —— 注意浮动体 `set_parent(..., Inherit::Yes)`（[L654-659](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L654-L659)）。

compose 阶段如何按 page/column 插入（仅供对照，详见 u4-l4）：

[`src/flow/compose.rs:65-80`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L65-L80) —— `Composer` 同时持有 `page_insertions` 与 `column_insertions` 两套插入区。

#### 4.5.4 代码实践

**实践目标**：弄清 `scope` 取值与「页级/列级插入」的对应。

**操作步骤**：

1. 在 `place.rs` 确认 `PlacementScope` 只有 `Column`/`Parent`（[L184-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L184-L190)）。
2. 在 `collect.rs` 的 `place()` 看到非浮动 + `Parent` 会被拒（[L308-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L308-L314)）。
3. 推论：只有**浮动**且 `scope=Parent` 的元素会成为「页级插入」（横跨所有列），其余浮动（`scope=Column`）是「列级插入」。

**需要观察的现象 / 预期结果**：写出映射——

| `float` | `scope` | compose 中的插入层级 |
| --- | --- | --- |
| false | Column | 绝对定位，原地放置（`Item::Placed`） |
| true | Column | 列级浮动插入（`column_insertions`） |
| true | Parent | 页级浮动插入（`page_insertions`，横跨所有列） |
| false | Parent | **非法**，collect 阶段 `bail!` |

#### 4.5.5 小练习与答案

**练习 1**：为什么浮动体排版后要 `set_parent(..., Inherit::Yes)`？

**参考答案**：浮动体被「拎出」正常流后，它的内容可能跨多帧（尤其大浮动体），需要包成一个带 parent 的 group，让内省器（u2-l4/u3-l5）能把它整体插到正确位置、且其内容继承父级样式；`Inherit::Yes` 正是允许这种样式继承（[L654-659](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L654-L659)）。

**练习 2**：`place(dx: 5pt, dy: 10pt)` 这种带位移的非浮动定位，`delta` 在哪里被用？

**参考答案**：在 collect 阶段被解析成 `Axes<Rel<Abs>>` 存进 `PlacedChild.delta`（[L318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L318)），真正施加位移是在 compose/distribute 把 `PlacedChild` 的 frame 摆到画布上时（本讲不展开）。

---

## 5. 综合实践

**任务**：给定下面这段 Typst 源码，预测它经过 realize 后产生的一组 `Pair`，再画出 `collect`（`FlowMode::Block`）输出的 `Child` 序列，并标出每个间距的 weakness 与全程的 `par_situation` 流转。

```typst
#v(8pt, weak: true)
第一段文字。
#block[一个不可断裂的小块]
第二段文字。
#colbreak()
第三段文字。
#place(float: true, top)[浮动]
```

**要求**：

1. 把每个原始元素映射到对应的 `Child` 变体（`Rel`/`Fr`/`Line`/`Single`/`Placed`/`Break`/`Tag`…）。
2. 标注 `#v(8pt, weak: true)` → `Child::Rel(8pt, 1)`；段间距 → `Child::Rel(_, 4)`；块因默认不可断裂 → `Child::Single`（除非显式 `breakable: true`）；`#colbreak()` → `Child::Break(false)` 且把 situation 重置为 `First`。
3. 用一张时序表写出：处理到第几个元素、当前 `par_situation`、push 了哪些 `Child`。
4. **进阶**：解释为什么两段文字之间的块间距（weakness 3）不会和段间距（weakness 4）简单相加——结合 4.3 的折叠规则说明谁覆盖谁。

> 预期结论草图（供你核对，建议先自己画再看）：序列大致为
> `[Rel(8pt,1), Rel(par_spacing,4), Line…, Rel(par_spacing,4), Rel(block_above,3), Single{小块}, Rel(block_below,3), Rel(par_spacing,4), Line…, Break(false), Rel(par_spacing,4), Line…, Placed{float}]`
> 其中块前后 `above/below` 默认 `Auto` 会回落到 `par.spacing`（weakness 4）——若你写成显式 `above/below` 则是 weakness 3。完成后再回到 4.3.4 验证相邻间距的折叠方向。

## 6. 本讲小结

- `collect` 是 `layout_flow` 三段式的第二步，把异构多态的 `Vec<Pair>` 一次性翻译成结构化的 `Vec<Child>`，并提前做完所有「与区域（高度）无关」的工作：解析样式、分配 locator、**给段落断行**、算 widow/orphan 预留。
- 切分原则：**宽度相关的工作在 collect 做一次；高度相关（能放几行、块怎么断）推迟到每区域的 compose/distribute。** 因此段落进 collect 就成了 `Child::Line`，而块只是打包成 `Single`/`Multi` 等待后续。
- `Collector` 通过 `to_packed::<T>()` 长链做类型分派；`par_situation`（First/Consecutive/Other）随段落、块、列断元素流转，决定首行缩进是否生效。
- `Child::Rel`/`Child::Fr` 携带 weakness `u8`（越小越强，0 强、1.. 弱），分级为「手动 v(0/1) < 块 fr(2) < 块 rel(3) < 自动/段落(4) < 行距(5)」，供 `Distributor` 在断点折叠相邻间距。
- 块按 `breakable`/`Fr` 分流为不可断裂的 `SingleChild`（单帧）与可断裂的 `MultiChild`（多帧 + `MultiSpill` 跨区域接力）；`expand.y &= alone` 保证只有独子块才纵向撑满。
- `PlacementScope` 只有 `Column`/`Parent` 两变体；`Parent` + 浮动在 compose 成为页级插入，`Column` + 浮动为列级插入，非浮动 + `Parent` 非法。大变体统一 bump-boxed 以控制 `Child` 枚举体积。

## 7. 下一步学习建议

本讲把 `Child` 的「生产侧」讲完了，接下来该看「消费侧」：

- **u4-l3（flow 分发 distribute）**：看 `Distributor` 如何逐个把 `Child` 放进当前区域、如何用本讲的 weakness 折叠间距、空间不足时如何返回 `Stop::Finish(forced)`。重点对照本讲 4.3 的 weakness 分级去读 `keep_weak_rel_spacing`。
- **u4-l5（块布局 block）**：深入 `layout_single_block`/`layout_multi_block`，看 `SingleChild`/`MultiChild` 真正调用的排版实现，以及 `MultiSpill` 在更底层的衔接。
- **u4-l4（flow 组合 compose）**：看 `PlacedChild` 的浮动体如何触发页级/列级插入与重排，把本讲 4.5 的 `scope` 映射落到实处。

建议带着本讲的「综合实践」产物（一张 Child 序列表）去读 distribute，逐行验证每个 `Child` 变体在消费时走了哪条分支。
