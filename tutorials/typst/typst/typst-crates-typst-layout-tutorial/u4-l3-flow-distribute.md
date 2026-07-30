# flow 分发 distribute：内容如何填入区域

## 1. 本讲目标

上一篇（u4-l2）我们看到了 `collect` 如何把 realized 的 `Pair` 列表一次性「拆包」成结构化的 `Child`。本讲紧跟其后，进入 `layout_flow` 三段式的第三步——**把 child 真正塞进当前 region**。

学完本讲，你应当能够：

- 说清 `distribute` 的「贪心分发」主循环：逐个消费 child、累计 `used`、不断削短 region 剩余高度。
- 解释一个不可断裂块在「剩余空间不足」时，`may_break` / `may_progress` 如何联手避免在过小区域上死循环地反复开新 region。
- 理解 sticky（粘性）块与 `Stop::Finish(forced)` 中 `forced` 布尔值的语义，以及粘性块为何要随后续帧一起迁移。
- 看懂列平衡（column balancing）如何借用 `balancing_target` 把 `distribute` 当作一把「测量尺」，通过反复重排收敛到等高。

本讲聚焦的两个最小模块是：**flow/distribute**（主角文件 `src/flow/distribute.rs`）与 **Distributor**（其中的状态结构体），并以 `src/flow/compose.rs` 作为调用方上下文。

## 2. 前置知识

本讲假设你已掌握前置讲义中的以下概念（不再重复展开）：

- **Regions / Region（pod）**（u2-l2）：`size`（剩余尺寸，逐元素削短）、`full`（整区域高度，相对尺寸基准）、`backlog`（后续候选高度队列）、`last`（可无限重复的末区高度）。四个关键方法：`next()` 推进队列、`may_break()` 判断能否换区域、`may_progress()` 判断换区域是否真的能改善空间、`is_full()` 要求「剩余 ≤ 0 且 may_progress」。
- **Frame / Fragment**（u2-l3）：排版产出是一棵 Frame 树，Fragment 是 `Vec<Frame>` 的新类型。
- **flow 三段式骨架**（u4-l1）：`configuration` → `collect` → 主循环（每区域调一次 `compose`）。主循环靠 `work.done() && (!regions.expand.y || regions.backlog.is_empty())` 终止，永远看不到 `Stop`，因为 `compose` 已在内部封装。
- **Child 类型族**（u4-l2）：`Child` 九变体枚举（`Tag`/`Rel`/`Fr`/`Line`/`Single`/`Multi`/`Placed`/`Flush`/`Break`）；不可断裂块 → `SingleChild`，可断裂块 → `MultiChild`（带 `MultiSpill` 跨区域接力）；间距带 weakness `u8`。

补充一个本讲会反复用到的术语：**`Stop`** 是 flow 内部的控制流事件枚举（定义在 `mod.rs`）：

```rust
// src/flow/mod.rs
enum Stop {
    /// 当前子区域应结束。false=空间不足，true=显式列断点。
    Finish(bool),
    /// 给定 scope 应重排（因插入 float/footnote 改变了可用空间）。
    Relayout(PlacementScope),
    /// 致命错误。
    Error(EcoVec<SourceDiagnostic>),
}
```

`FlowResult<T> = Result<T, Stop>`。本讲主角 `distribute` 会吞掉 `Stop::Finish`（在内部消化为布尔 `forced`），只让 `Relayout` 与 `Error` 继续上浮。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/flow/distribute.rs` | **主角**。`distribute()` 入口、`Distributor` 状态机、`Item` 枚举、贪心主循环 `run()`、各类 child 的分发方法、`finalize()` 组装帧。 |
| `src/flow/compose.rs` | 调用方。`column_contents()` 调 `distribute()`；列平衡在此驱动 `balancing_target` 并通过 `Stop::Relayout` 反复重排。 |
| `src/flow/mod.rs` | 提供 `Work`（待办状态）、`Stop`、`FlowResult`，以及消费 `distribute` 产物的外层 `layout_flow` 主循环。 |
| `src/flow/collect.rs` | 提供 `Child` / `SingleChild` / `MultiChild` / `LineChild` / `PlacedChild` 的定义，`distribute` 消费的就是这些。 |
| `crates/typst-library/src/layout/regions.rs` | `Regions` 的 `is_full` / `may_break` / `may_progress` / `next` 的真实定义（跨 crate 引用）。 |

## 4. 核心概念与源码讲解

### 4.1 贪心分发：把 child 逐个塞进当前 region

#### 4.1.1 概念说明

`distribute` 要解决的问题是：给定一组已经 `collect` 好的 `Child` 和一个**当前 region**，把「能放下的」child 一个个放进去，产出这一区域对应的 `Frame`，以及内部内容实际占用的高度（后者用于列平衡）。

它的策略非常朴素——**贪心**（greedy）：从队首取一个 child，试着放进当前 region；放得下就放（累计已用高度、削短剩余高度），放不下就触发区域结束。它不做全局最优回溯，只看「当前这一个能不能塞」。

这里有一个关键的分工：`distribute` 只负责「往一个 region 里填」，而「填不下后换下一个 region」由外层 `layout_flow` 主循环通过 `regions.next()` 完成。`distribute` 通过返回 `Stop::Finish` 来告诉调用方「这块区域满了，该结束了」。

#### 4.1.2 核心流程

```
distribute(composer, regions, balancing_target):
  1. 构造 Distributor { composer, regions(会被不断削短), items=[], used=0,
                        target=balancing_target, sticky=None, stickable=None }
  2. 记下初始快照 init（用于「全可迁移」时回滚）
  3. forced = run() 的结果：
       - run() 正常结束 Ok(())  → forced = work.done()
       - run() 返回 Finish(forced) → forced = 该布尔值
       - run() 返回 Relayout/Error → 直接上浮
  4. 用初始 region 尺寸重建一个 Region，调 finalize(region, init, forced)
     → 返回 (Frame, 实际占用高度)
```

`run()` 内部是一个紧凑的两段式：

```
run():
  a. 若 work.spill（上一区域可断裂块没排完的剩余）非空，先排它（multi_spill）
  b. while work.head() 还有 child:
        child(head)            // 分派到 tag/rel/fr/line/single/multi/placed/flush/break
        work.advance()         // 队首出队
```

`child()` 内部对每个 `Child` 变体做类型分派；任何「放不下」的方法都会 `return Err(Stop::Finish(..))`，由于 `?` 运算符，这会立刻从 `run()` 冒泡出去（跳出 while 循环），交给第 3 步捕获。

#### 4.1.3 源码精读

入口 `distribute()` 是一个薄封装，它构造 `Distributor`、跑 `run()`、再用 `finalize()` 组装：

[distribute.rs:15-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L15-L37) —— 构造 `Distributor`，调用 `run()`，把 `Stop::Finish` 吞成 `forced` 布尔值，最后 `finalize` 组装帧。

注意第 31 行 `Ok(()) => distributor.composer.work.done()`：`run()` 正常走完只代表「children 列表消费完」，但 `work` 可能还压着 floats/footnotes，所以 `forced` 取的是 `work.done()`，而「不一定是 true」——这一点 4.3 节会用到。

`Distributor` 是分发的全部状态：

[distribute.rs:42-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L42-L76) —— `regions`（随填入不断削短）、`items`（已排好待对齐的项）、`used`（已用尺寸）、`target`（列平衡目标）、`sticky`/`stickable`（粘性块快照）。

`run()` 主循环极其精炼：

[distribute.rs:119-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L119-L133) —— 先处理跨区域接力来的 `spill`，再 `while head()` 逐个消费 child，直到无 child 或某个 child 触发 `Finish`。

`child()` 做纯类型分派：

[distribute.rs:142-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L142-L155) —— 把九种 `Child` 变体分派到对应方法；带 `?` 的方法可能触发 `Stop`。

「塞进 region」的本质是下面这个 4 行函数——同时削短剩余高度、累加已用高度：

[distribute.rs:172-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L172-L175) —— `regions.size.y -= amount`（剩余变少），`used.y += amount`（已用变多）。这就是「贪心填入」的最小动作。

`items` 里放的是 `Item` 枚举，它是 `Child` 排版后的「半成品」：

[distribute.rs:86-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L86-L97) —— `Tag`/`Abs(绝对间距, weakness)`/`Fr(分数间距或块, weakness, Option<SingleChild>)`/`Frame(帧, 对齐)`/`Placed(帧, placed)`。注意 `Fr` 的第三个字段：`None` 是纯分数间距，`Some` 是「分数高度的块」（`height: 1fr` 的 block），后者在 `finalize` 时要按份额重新排版。

#### 4.1.4 代码实践

**目标**：在脑中（或纸上）模拟一次完整的贪心分发，确认「逐 child 消费 + 削短 region」的机制。

**操作步骤**：

1. 阅读上面的 [distribute.rs:119-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L119-L133) 与 [distribute.rs:172-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L172-L175)。
2. 假设有这样一个 children 序列（均为 `SingleChild`，高度已测好）：`[A:30pt, B:20pt, C:40pt]`，当前 region 剩余 `regions.size.y = 70pt`、`full = 70pt`、`backlog = [70pt]`、`last = None`。
3. 手动跟踪 `run()` 每一步的 `used.y` 与 `regions.size.y`：
   - 取 A(30pt)：放得下 → `used.y=30`，`size.y=40`，`advance`。
   - 取 B(20pt)：放得下 → `used.y=50`，`size.y=20`，`advance`。
   - 取 C(40pt)：`fits(40)`？`size.y=20` 放不下 40 → 触发 `Stop::Finish`（具体见 4.2 节），跳出 while。

**需要观察的现象**：`used.y` 单调递增、`regions.size.y` 单调递减；C 没有被消费（`work.advance()` 未执行），它会留在 `work.children` 里，由外层 `layout_flow` 调 `regions.next()` 进入下一个 region 后再次 `compose` 时重新分发。

**预期结果**：第一区域只装下 A、B 共 50pt；C 进入第二区域。这与 Typst 实际分页行为一致。

> 说明：本实践为源码阅读型，未运行编译命令；如需验证，可临时在 `run()` 的 `child(child)?` 前后插入 `eprintln!("used={:?} size={:?}", self.used.y, self.regions.size.y);`，再用 `cargo test` 跑 flow 相关测试观察输出。**待本地验证**具体数值。

#### 4.1.5 小练习与答案

**练习 1**：`distribute` 处理完所有 child 后 `run()` 返回 `Ok(())`，但 `work.done()` 仍可能为 `false`，为什么？

**答案**：`run()` 只把 `children` 列表消费完，但 `work` 可能还压着待处理的 `floats` / `footnotes` / `spill`。`distribute` 在第 31 行把 `Ok(())` 映射成 `forced = work.done()`，若仍有待处理插入项，则 `forced=false`，`finalize` 据此允许把可迁移项迁到下一区域（见 4.3）。

**练习 2**：`Item::Fr` 的第三个字段 `Option<&'b SingleChild<'a>>` 表示什么？

**答案**：`None` 表示纯分数间距（如 `#v(1fr)`）；`Some(single)` 表示一个「分数高度」的块（`block(height: 1fr)`），它先以占位形式进入 `items`，`finalize` 时再按 `Fr::share` 算出实际高度并重新排版该块（见 [distribute.rs:582-592](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L582-L592)）。

---

### 4.2 放不下怎么办：may_break / may_progress 与强制换区域

#### 4.2.1 概念说明

这是本讲的**核心难点**，也是规格指定的实践任务所在。

考虑一个**不可断裂块**（`SingleChild`，比如一张图片或 `block(breakable: false)`）高度 50pt，而当前 region 剩余只有 10pt。直觉上「放不下就该换区域」。但「换区域」这个动作必须是有意义的——下一个区域得比当前区域「更大或至少不同」，否则换区域就是在原地踏步。

这正是 `Regions` 两个方法的分工（定义在 `typst-library`）：

- `may_break()`：**还有没有下一个区域可换**（`backlog` 非空或 `last` 存在）。
- `may_progress()`：**换到下一个区域能否改善空间不足**（`backlog` 非空，或 `last` 存在且其高度 ≠ 当前 `size.y`）。

注意二者不等价：当队列只剩 `last`（可无限重复的末区）且 `last` 高度恰好等于当前 `size.y` 时，`may_break()=true`（确实能 `next()`），但 `may_progress()=false`（换过去尺寸一模一样，毫无改善）。

`distribute` 的关键守卫就是：**只有当 `may_progress()` 为真时，才因为「放不下」而触发 `Finish`**。否则，即便放不下，也硬塞进去（让内容溢出区域边界），从而保证流程能向前推进、最终终止。

#### 4.2.2 核心流程

以不可断裂块 `single()` 为例的判定：

```
single(child):
  frame = child.layout(base)            // 用 base 高度测真实尺寸
  if child 是分数尺寸(fr): 走 Item::Fr 占位分支，return
  if !fits(frame.height) && may_progress():
      return Finish(false)              // 换区域可能改善 → 结束当前区域
  // 否则（放得下，或 may_progress=false 无法改善）：
  frame(frame, align, sticky, breakable=false)   // 硬塞，可能溢出
```

`fits(amount)` 判定「当前 region 剩余是否容得下 amount」：

```
fits(amount):
  regions.size.y.fits(amount)                              // 剩余高度够
  && target.is_none_or(|t| t.fits(used.y))                 // 且未超列平衡目标
```

可断裂块 `multi()` 与行 `line()` 遵循同样的 `may_progress` 守卫思想（细节在 4.2.3）。

**为什么这能防死循环**？设想没有 `may_progress` 守卫，代码变成 `if !fits(...) { return Finish(false); }`。在「最后一个可重复区域」（`may_progress()=false`）上，一个过高块会反复触发 `Finish`；外层 `layout_flow` 收到帧后 `regions.next()`，但队列已空、`last` 又与当前同高，于是**在尺寸完全相同的 region 上重新 `compose` 同一个过高块**——`Finish` 再触发……无限循环。守卫的存在让「无法改善时」改为接受溢出，块被塞入、`work.advance()` 推进、最终 children 耗尽、`work.done()` 为真、循环终止。

#### 4.2.3 源码精读

不可断裂块 `single()`：

[distribute.rs:328-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L328-L351) —— 注意第 330-333 行用 `regions.base()`（整区域高度）而非 `regions.size`（剩余高度）去测块的真实尺寸；第 346-348 行是关键的 `may_progress` 守卫：放不下且能改善才 `Finish`，否则落到第 350 行硬塞。

注意 `single.layout` 收到的是 `Region::new(self.regions.base(), self.regions.expand)`，即「用整区域高度测量」——这样块的尺寸不会因为前面内容挤占而被低估。真正的「放得下吗」由第 346 行 `fits(frame.height())` 用剩余 `size.y` 判断。

`fits()` 本体：

[distribute.rs:293-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L293-L300) —— 剩余高度够 **且**（无平衡目标 或 已用高度未超目标）。第二项是 4.4 节列平衡用的。

可断裂块 `multi()` 多了一步「先看 region 是否已满」：

[distribute.rs:354-392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L354-L392) —— 第 365-367 行 `pod.is_full()` 直接 `Finish`（`line`/`single` 通过 `fits` 隐式做了这件事）；第 370 行排版，若产生 `spill`（第 385 行）说明没排完，存入 `work.spill` 并 `Finish` 让下一区域接力。

`Regions` 的三个判定方法真实定义（跨 crate，在 typst-library）：

[regions.rs:98-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L98-L111) —— `is_full()` = 「剩余 ≤ 0 且 may_progress」；`may_break()` = 「有 backlog 或有 last」；`may_progress()` = 「有 backlog，或有 last 且 last ≠ 当前 size.y」。最后一行正是区分「能换」与「换了有意义」的关键。

#### 4.2.4 代码实践（本讲指定实践任务）

**目标**：追踪一个不可断裂块在「剩余空间不足」时的两种分发行为，亲手验证 `may_break` / `may_progress` 如何避免在过小区域上死循环地不断开新 region。

**操作步骤**：

1. 打开 [distribute.rs:328-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L328-L351)（`single`）与 [regions.rs:98-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L98-L111)（三个判定）。
2. 设有一个 50pt 高的不可断裂块 `B`，前面已用掉空间使当前 region 剩余 `size.y = 10pt`。分两种 region 配置分别推演：

   **情形 A（能改善）**：`full=100pt`、`backlog=[100pt]`、`last=None`。
   - `single` 第 330 行用 `base()=(?, 100)` 测得 `frame.height()=50`。
   - 第 346 行：`fits(50)` → `size.y=10` 放不下 50 → `false`；`may_progress()` → backlog 非空 → `true`。条件成立 → `return Finish(false)`。
   - 外层 `layout_flow` 把当前帧收下，`regions.next()` 推进到 100pt 的新区域，重新 `compose` → 此时 `fits(50)` 成立，B 被放入。**正常分页。**

   **情形 B（无法改善，最后一个可重复区域）**：`full=100pt`、`backlog=[]`、`last=Some(10pt)`（即 last 与当前 size.y 同为 10pt——模拟「页面正文高度被前面内容削到只剩这么点、且没有更多页」的退化情形）。
   - `single` 同样测得 `frame.height()=50`。
   - 第 346 行：`fits(50)` → `false`；`may_progress()` → backlog 空、`last=Some(10)` 但 `size.y=10 == 10` → **`false`**。条件不成立 → **不 Finish**。
   - 落到第 350 行 `self.frame(...)`，B 被硬塞进 10pt 剩余的区域（`use_height(50)` 使 `size.y` 变为 −40pt，溢出），`work.advance()` 推进。
   - 若 B 是最后一个 child：`run()` 返回 `Ok(())`，`work.done()=true` → 终止。**没有死循环。**

3. （可选，**待本地验证**）在 `single` 的第 346 行之前插入：
   ```rust
   eprintln!("single fits={} may_progress={} size.y={:?} frame_h={:?}",
       self.fits(frame.height()), self.regions.may_progress(),
       self.regions.size.y, frame.height());
   ```
   编写一个使块明显高于页面的 Typst 文档（如 `#page(height: 20pt; block(breakable: false)[#image("huge.png")])`）编译运行，观察日志中 `may_progress=false` 分支被命中、且程序正常退出而非卡死。

**需要观察的现象**：情形 A 中 `may_progress=true` 时走 `Finish`、内容正常进入下一页；情形 B 中 `may_progress=false` 时**跳过 Finish**、块被溢出式放入、流程终止。

**预期结果**：`may_progress` 是「能否换一个更好的区域」的判据。它为真时才因空间不足换区域；为假时接受溢出，从而保证 `layout_flow` 主循环必然收敛——这正是「过小区域上不死循环」的根本机制。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `single()` 用 `regions.base()` 测块尺寸，却用 `regions.size.y`（经 `fits`）判放得下吗？

**答案**：不可断裂块要在「整区域高度」上测出真实自然高度（否则会被前面内容挤出的剩余高度低估，导致尺寸错误）；而「当前这一区域还放得下吗」必须看已被削短的剩余 `size.y`。二者职责不同。

**练习 2**：若把第 346 行的 `&& self.regions.may_progress()` 删掉，在 `backlog=[]`、`last=Some(h)` 且 `size.y=h` 的末区里一个过高块会怎样？

**答案**：会反复 `Finish(false)`；外层 `layout_flow` 调 `regions.next()`，但 `last` 与 `size.y` 同高、`work` 未消耗（块没被 `advance`），于是**在尺寸相同的 region 上无限重排同一块**——死循环。`may_progress` 守卫正是为此而设。

---

### 4.3 sticky 粘性块与 forced 断点

#### 4.3.1 概念说明

**sticky（粘性）块**是一类特殊块（如 `block(sticky: true)` 的标题），语义是「必须与紧随其后的内容呆在同一区域」。如果一段粘性标题恰好落在某页底部、而它之后的正文跑到了下一页，就会产生「孤立的标题」——排版上很糟糕。`distribute` 用快照机制保证：当一区域恰好结束在一组粘性块之后时，把整组粘性块连同后续帧一起**迁移到下一区域**。

**forced 断点**指 `Stop::Finish(bool)` 中的布尔值，它编码了「这次区域结束的成因」：

- `Finish(true)`：**显式**列断点（用户写了 `#colbreak()`），或 flow 彻底结束。
- `Finish(false)`：**自然**断点（空间不足、或有待处理的 floats/footnotes）。

`forced` 在 `finalize` 中决定：是否把当前区域的某些尾部内容迁移到下一区域。显式断点意味着「此处就是定稿」，不迁移；自然断点则可能迁移。

#### 4.3.2 核心流程

```
frame(frame, align, sticky, breakable):
  if sticky 且 还没存快照 且 本组粘性「值得迁移」(stickable):
      sticky = snapshot()                  // 在这个粘性块之前打点
  ... 处理 footnotes ...
  if 非 sticky 且 frame 非空:
      sticky = None; stickable = None      // 遇到非粘性帧，结束本组、重置
  use_height(frame.height()); push Item::Frame

finalize(forced):
  if forced:          flush 标签            // 定稿，不迁移
  else if 全部 items 都 migratable:  restore(init)   // 整区域只有可迁移项 → 全迁走
  else if 存在 sticky 快照:          restore(sticky) // 尾部是一组粘性块 → 迁走这组
```

`stickable` 是个微妙的三态守卫（`Option<bool>`），专门处理「粘性块位于区域最顶部」的边界情形——见 4.3.3。

#### 4.3.3 源码精读

粘性块快照的打点与重置在 `frame()` 内：

[distribute.rs:449-477](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L449-L477) —— 第 449-454 行：遇到 sticky 块且尚无快照时，用 `stickable.get_or_insert_with(|| may_progress())` 决定本组是否值得迁移，值得则在块前打 `snapshot()`；第 471-477 行：遇到非粘性帧则丢弃快照并重置 `stickable`（一组粘性块被「打断」了）。

`stickable` 字段的注释把边界情形讲得很清楚：

[distribute.rs:55-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L55-L74) —— 第一组粘性块中，若 `may_progress()` 为假（块在区域最顶部，迁移只会回到同样满的区域顶部，等于死循环），则 `stickable` 置 `Some(false)`，**禁用本组粘性**，直到遇到首个非粘性帧才重置为 `None`。注释承认这在「粘性块+其后续帧在一页内都放不下」的病态情形下会留下孤立标题，但那已无解。

`finalize` 开头依据 `forced` 做迁移决策：

[distribute.rs:545-560](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L545-L560) —— `forced` 时 flush 标签（定稿）；否则若全部项 `migratable` 则回滚到初始（整区域只有标签/零尺寸帧/非浮动 placed，全迁走）；否则若尾部有粘性快照则回滚它（粘性组连同后续帧迁到下一区域）。

`migratable` 定义了「哪些项可以无损迁到下一区域」：

[distribute.rs:99-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L99-L115) —— 纯标签、只含标签/链接的零尺寸帧、非浮动的 placed 元素可迁移；真实内容帧与浮动元素不可迁移。

显式列断点 `break_()` 产生 `Finish(true)`：

[distribute.rs:525-534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L525-L534) —— 当存在后续区域（`backlog` 或 `last`）且（非 weak 或当前已有内容）时，`advance` 后返回 `Finish(true)`，即显式断点。

#### 4.3.4 代码实践

**目标**：用一个真实 Typst 文档验证粘性块「随后续帧迁移」的行为。

**操作步骤**：

1. 编写文档（**示例代码**，非项目原有）：
   ```typ
   #set page(height: 60pt)
   #show heading: it => block(sticky: true, it)
   = 标题 A
   第一段正文，足够长足够长足够长足够长足够长足够长。
   = 标题 B
   第二段正文。
   ```
2. 编译，观察「标题 B」是否与「第二段正文」出现在同一页（即若标题 B 落在第 1 页底部、正文在第 2 页，则二者应被一起迁移到第 2 页，而不是标题 B 孤悬第 1 页底）。
3. 对照 [distribute.rs:545-560](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L545-L560) 理解：第 1 页 `finalize` 时 `forced=false` 且存在 `sticky` 快照 → `restore(snapshot)` 把「标题 B」从第 1 页回滚掉、迁到第 2 页。

**需要观察的现象**：粘性标题始终与其后第一段正文同页。

**预期结果**：与上述一致；若把 `sticky: true` 去掉，标题会留在原页底部、正文跑到下页（即出现孤立标题）。**待本地验证**具体分页位置。

#### 4.3.5 小练习与答案

**练习 1**：sticky 块何时会被「禁用粘性」？

**答案**：当一组连续 sticky 块的第一个位于区域顶部、且 `may_progress()` 为假（迁移回去仍是无改善的同尺寸区域、会导致死循环）时，`stickable` 被置为 `Some(false)`，整组粘性禁用，直到遇到首个非粘性帧重置为 `None`（见 [distribute.rs:55-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L55-L74)）。

**练习 2**：`Stop::Finish(true)` 与 `Finish(false)` 在 `finalize` 中如何区别处理？

**答案**：`forced=true`（显式断点或 flow 结束）时只 flush 待处理标签，**不迁移**任何项——断点是定稿；`forced=false`（自然断点）时，若全部项可迁移则整体回滚到初始快照，否则若尾部是粘性组则回滚粘性快照，把尾部内容迁到下一区域。

---

### 4.4 列平衡测量：balancing_target

#### 4.4.1 概念说明

「列平衡」指让多栏文档的各栏高度尽量相等（而不是第一栏拉满、后面几栏空荡荡）。Typst 用 `columns(balanced: true)` 开启。

难点在于：要算出「平衡高度」，得先知道「在这个高度下各栏能放多少内容」；而「能放多少」又取决于这个高度——**鸡生蛋**。

Typst 的解法是把 `distribute` 当作一把**测量尺**：给它一个 `balancing_target`（候选平衡高度），让它在前若干栏只填到这个高度为止，量出总用量；据此算出平均高度；若与上次不同就反复重排，直到收敛。这本质上是固定点迭代。

#### 4.4.2 核心流程

```
column()（compose.rs，每栏调一次）:
  balancing_target =
    若 当前栏 < 最后一栏:  column_balancing_height - float_height   // 限制前若干栏
    否则(最后一栏):        None                                     // 末栏吃下全部剩余
  loop（列内重排 checkpoint）:
    column_contents(pod, balancing_target) → (frame, used_height)
    ...

page_contents()（compose.rs，所有栏排完后）:
  if 配置开启 balanced 且 work.done():
      height = 各栏总用量 total_used_height / 栏数 count     // 平均高
      if column_balancing_height 还没设 或 小于 height:
          column_balancing_height = height
          return Relayout(Parent)                            // 整页重排，用新目标再量一遍
```

`distribute` 端只需在 `fits()` 里多判一个 `target`：

```
fits(amount):
  size.y 够 amount
  && (target 为 None 或 used.y ≤ target)        // 列平衡下，累计用量不超过目标
```

注意 [distribute.rs:296-299](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L296-L299) 的注释：判断用的是 `target.fits(self.used.y)`（**不含** amount 本身），这样可避免凸出的项都堆到最后一栏。

#### 4.4.3 源码精读

调用方在 `compose.rs` 的 `column()` 中构造 `balancing_target`：

[compose.rs:208-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L214) —— 前若干栏目标 = `column_balancing_height - float_height`（只扣除浮动体高度，不扣脚注）；最后一栏目标为 `None`（承担剩余全部内容）。

整页排完后在 `page_contents()` 判定是否需要重排：

[compose.rs:173-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180) —— `balanced && work.done()` 时算 `total_used_height / count`；若新算的 `height` 比已存的 `column_balancing_height` 更大，则更新并 `return Err(Stop::Relayout(PlacementScope::Parent))`，触发整页重排。

`column_contents()` 把目标透传给 `distribute`：

[compose.rs:263-279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L263-L279) —— 第 278 行 `distribute(self, regions, balancing_target)`，是 `distribute` 与列平衡的唯一接合点。

`distribute` 端，`fits()` 与 `multi()` 都尊重 `target`：

[distribute.rs:293-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L293-L300) —— `target.is_none_or(|t| t.fits(self.used.y))`，即累计用量未超目标才继续放。

[distribute.rs:357-361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L357-L361) —— 可断裂块排版前，若设了 target，把 pod 高度压到 `target - used.y`（`set_min`），让块「以为」区域只有这么高，从而测量出在该高度下的断行。

`distribute` 返回的第二个值 `used_height_without_fr`（[distribute.rs:562](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L562)、[L667](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L667)）正是回传给 `column()` 的「实际占用高度」（不含 fr 间距），用于 `page_contents()` 算平均高——这就是 `distribute` 返回 `(Frame, Abs)` 中那个 `Abs` 的用途。

#### 4.4.4 代码实践

**目标**：跟踪一次列平衡的迭代过程，理解「测量—算平均—重排」的收敛。

**操作步骤**：

1. 阅读 [compose.rs:173-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180) 与 [compose.rs:208-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L214)。
2. 设两栏（`count=2`）、`balanced=true`，正文总高度 300pt。
3. 推演：
   - **第 1 次排版**：`column_balancing_height=None`，两栏都无 target → 第 1 栏拉满（比如 300pt 全进第 1 栏，第 2 栏空），`total_used_height=300`。
   - 算 `height = 300/2 = 150`；`column_balancing_height` 从 None → 150，`return Relayout(Parent)`。
   - **第 2 次排版**：第 1 栏 target=150 → `distribute` 只往第 1 栏放约 150pt 内容；第 2 栏 target=None 吃下剩余 150pt。`total_used_height≈300`，`height≈150`。
   - `column_balancing_height(150)` 已不 `< height(150)` → 不再 Relayout，**收敛**。
4. （可选，**待本地验证**）在 `page_contents` 第 175 行后插 `eprintln!("balancing: height={:?} stored={:?}", height, self.column_balancing_height);`，编译一个 `#columns(2, balanced: true)[ ...长文本... ]` 文档，观察日志出现 1~2 次后稳定。

**需要观察的现象**：`column_balancing_height` 单调不减，迭代少数几轮后稳定，最终各栏高度接近。

**预期结果**：平衡高度收敛到约等于「总内容高 / 栏数」。

#### 4.4.5 小练习与答案

**练习 1**：为什么最后一栏的 `balancing_target` 是 `None`？

**答案**：前若干栏受 target 限制以「测量在目标高度下能放多少」；最后一栏承担「剩余全部内容」，若也设上限会把内容挤垮。`None` 表示不限制（见 [compose.rs:210-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L210-L214)）。

**练习 2**：列平衡为何需要多次 `Relayout`？

**答案**：第一次无 target 测出总用量并算出平均高；以此为目标重排会改变各栏用量分布，可能改变总用量，因此需迭代直到 `column_balancing_height` 不再增大（即固定点）。这是用「测量尺」逼近平衡高度的必然代价。

---

## 5. 综合实践

把本讲四个知识点串起来，做一个端到端的追踪任务。

**场景**：一个两栏、开启列平衡的文档，正文里含一个粘性的章节标题、一段普通正文、以及一个高度超过半栏的不可断裂图块。

**任务**：

1. 画出从 `layout_flow` 主循环 → `compose` → `column_contents` → `distribute` 的调用栈，标出 `balancing_target` 如何从 `page_contents` 一路传到 `distribute.fits`。
2. 对那个不可断裂图块，分别在「第 1 栏剩余充足」与「已到末栏且剩余不足」两种情况下，判定 `single()` 走 `Finish` 还是硬塞（用 `may_progress` 解释）。
3. 说明若该图块前正好是粘性标题、且区域恰好在图块前结束，`finalize` 会如何 `restore(sticky)` 把标题连同图块一起迁到下一区域。
4. 解释为何整个过程中 `distribute` 内部的 `Stop::Finish` 不会上浮到 `layout_flow`，而 `Stop::Relayout`（列平衡触发）会上浮并在 `page()`/`column()` 的 checkpoint 循环里被吞掉。

**参考要点**：

- 调用栈：`layout_flow`（[mod.rs:223-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L223-L234)）→ `compose` → `page_contents`（算 `column_balancing_height`，[compose.rs:173-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180)）→ `column`（构造 target，[compose.rs:208-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L214)）→ `column_contents` → `distribute`（[compose.rs:278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L278)）→ `fits` 用 target（[distribute.rs:293-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L293-L300)）。
- 不可断裂块：第 1 栏剩余充足 → `fits` 真，直接放；末栏不足且 `may_progress=false` → 跳过 `Finish`、硬塞溢出（[distribute.rs:346-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L346-L350)）。
- 粘性迁移：`frame()` 打 `sticky` 快照（[distribute.rs:449-454](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L449-L454)），`finalize` 在 `forced=false` 时 `restore(snapshot)`（[distribute.rs:555-557](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L555-L557)）。
- `Finish` 在 `distribute` 入口被吞成 `forced`（[distribute.rs:30-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L30-L34)），不上浮；`Relayout` 上浮到 `column`/`page` 的 checkpoint 循环被 `*self.work = checkpoint.clone()` 吞掉（[compose.rs:98-100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L98-L100)、[L219-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L219-L221)）。

## 6. 本讲小结

- `distribute` 用**贪心策略**把 `collect` 产出的 `Child` 逐个塞进当前 region：每塞一个就 `use_height`（削短 `regions.size.y`、累加 `used.y`），放不下则 `Stop::Finish`，主循环 `run()` 只消费 `children` 列表。
- **`may_progress()` 是防死循环的核心守卫**：只有「换区域能改善空间」时才因放不下而 `Finish`；在末区无法改善时改为硬塞（接受溢出），保证 `layout_flow` 主循环必然收敛。这正是规格指定实践任务的答案。
- **sticky 粘性块**用快照机制随后续帧一起迁移到下一区域；`stickable` 三态守卫在「粘性块位于区域顶部且无法改善」时禁用粘性，避免死循环；`Stop::Finish(forced)` 中 `true`=显式断点/定稿、`false`=自然断点，决定 `finalize` 是否迁移尾部内容。
- **列平衡**把 `distribute` 当测量尺：`compose` 传入 `balancing_target` 让前若干栏只填到目标高度，量出总用量算平均高，再 `Stop::Relayout(Parent)` 反复重排直到 `column_balancing_height` 收敛；`distribute` 返回的 `Abs`（实际占用高度，不含 fr）正是回传给平衡计算的量。
- `Stop::Finish` 在 `distribute` 入口被吞成 `forced` 布尔、不上浮；只有 `Relayout`（float/footnote/列平衡）与 `Error` 继续上浮，分别由 `column`/`page` 的 checkpoint 循环与外层错误处理消费。

## 7. 下一步学习建议

本讲讲完了「往一个 region 里填」的 `distribute`。接下来的讲义：

- **u4-l4 flow 组合 compose：浮动体与脚注**：深入 `compose.rs` 的 page/column 两层 checkpoint 循环，看 `Stop::Relayout` 如何在加入 float/footnote 后缩小可用 region 并触发重新 `distribute`——这是本讲反复提到的 `Relayout` 上浮后的真正消费者。
- **u4-l5 块布局 block：单块与可断裂多块**：展开 `SingleChild`/`MultiChild` 背后的 `layout_single_block`/`layout_multi_block` 与 `MultiSpill` 跨区域接力机制，补全「块内部如何排版」这一侧。
- 若对列平衡的数学收敛感兴趣，可回头重读 [compose.rs:173-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180) 并尝试证明该迭代单调有界、必然收敛。
