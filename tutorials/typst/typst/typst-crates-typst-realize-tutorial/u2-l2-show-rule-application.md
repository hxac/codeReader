# show 规则的应用流程 visit_show_rules

## 1. 本讲目标

上一讲（u2-l1）我们把 realize 的可变工作台 `State` 拆了个底朝天，其中 `outside` 标志那句「进入 show 规则输出时被压低、返回时恢复」埋了个伏笔——它就发生在本讲的主角 [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 里。在 u1-l3 的 8 步流水线中，`visit_show_rules` 是**第 3 道关卡**：它负责「给当前元素套用 show 规则、并在套用前把元素准备好」。

本讲聚焦这一道关卡。学完本讲，你应该能够：

- 说清 [`Verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180) 与 [`ShowStep`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L183-L188) 这两个结构/枚举的作用：前者是「要不要处理、怎么处理」的判决书，后者是判决书里「具体套哪条规则」的那一栏。
- 区分 show 规则的两条执行路径：用户写的 [`Recipe::apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L489-L511) 与内置的 [`NativeShowRule::apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1122-L1132)，理解它们各自的输入、输出与差异。
- 解释为什么 show 规则里抛出的错误**不会立即终止编译**，而是被 [`engine.delay`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L38-L50) 暂存成「延迟错误」。
- 看懂 show 输出如何带上 `start`/`end` tag、如何在 `outside`/show 深度的「外壳」保护下重新喂回流式流水线。

本讲是 u2 系列承上启下的一环：它把 u2-l1 的 `State` 与下一讲 u2-l3 的 `verdict`、u2-l4 的 `prepare` 串成一条完整链路。

## 2. 前置知识

本讲承接 u1-l3 与 u2-l1，假定你已经了解：

- **`visit()` 的 8 步流水线与短路语义**（u1-l3）：`visit_show_rules` 是第 3 步，返回 `true` 表示「我认领了这个元素，后续步骤别管了」。
- **`State` 工作台**（u2-l1）：`engine`/`locator`/`arenas` 是配置型字段，`outside` 是工作区标志。本讲会反复读写 `s.outside` 与 `s.engine`。
- **`Pair<'a> = (&'a Content, StyleChain<'a>)`** 与 **`StyleChain`**（u1-l2）：样式链是把多层样式叠在一起读取的「链表」，show 规则（`Recipe`）就挂在链上。

本讲新引入几个概念，先通俗解释：

- **show 规则（show rule）**：Typst 源码里 `show 标的: 转换` 写出来的东西。它把「匹配某个标的的元素」替换成「转换后的新内容」。比如 `show heading: it => [★ #it.body ★]` 会让所有标题前后加星号。
- **show-set 规则**：一种特殊的 show 规则，写作 `show 标: set ...`。它不替换内容，而是**给元素附加样式**。在 realize 里它走的是与普通 show 规则不同的路径（折进 `map`，而非产出 `ShowStep`）。
- **内置 show 规则（native show rule）**：不是用户写的，而是每个原生元素（如 `heading`、`list`）在 Rust 里自带的「默认外观」函数。用户没写 show 规则时，就靠它把元素渲染成默认形态。
- **判决（verdict）**：先别急着套规则，先「审一审」这个元素——它有没有匹配的 show 规则？要不要准备（prepare）？要不要附加 show-set 样式？审完给出一份结论，这就是 `Verdict`。
- **guard（防重入位）**：show 规则的输出可能再次匹配同一条 show 规则，从而无限递归。为防止这一点，realize 给元素打上一个「这条规则已经套过了」的标记位，下次再遇到就跳过。

## 3. 本讲源码地图

本讲主要在两个文件之间穿梭：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | `visit_show_rules`、`Verdict`、`ShowStep` 与衔接用的 `verdict`/`prepare` 都在这里。 |
| [crates/typst-library/src/foundations/styles.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs) | `Recipe`、`Transformation`、`RecipeIndex`、`NativeShowRule` 的定义与 `apply` 实现。 |
| [crates/typst-library/src/foundations/content/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs) | `is_guarded`/`guarded`/`is_prepared`/`mark_prepared` 等 content 辅助方法。 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | `engine.delay`（延迟错误）与 `route.increase`/`check_show_depth`（show 深度防线）。 |

> 链接基准 HEAD：`32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。下面所有永久链接均基于此 HEAD。

## 4. 核心概念与源码讲解

### 4.1 Verdict 与 ShowStep：套不套、套哪条

#### 4.1.1 概念说明

`visit_show_rules` 一上来要做一个决定：**这个元素到底要不要被 show 规则碰？** 这个决定不是在 `visit_show_rules` 里现场做的，而是先交给一个叫 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L530) 的函数去「审理」，审理结果装在一个 [`Verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180) 结构体里交回来。

`Verdict` 可以理解成法官的「判决书」，上面写三件事：

- **`prepared`**：这个元素之前有没有被「准备」过？准备（prepare）是只该做一次的事（分配唯一位置、合成字段、生成 tag 等）。判决书带上这个标记，是为了告诉 `visit_show_rules`「这次要不要再 prepare 一遍」。
- **`map`**：要附加到这个元素上的**样式集合**（主要来自 show-set 规则）。注意它是「只改样式、不换内容」的部分。
- **`step`**：一个 `Option<ShowStep>`。如果它是 `Some`，就表示「找到了一条会**替换内容**的 show 规则，具体是哪条见 `ShowStep`」；如果是 `None`，表示没有任何替换型规则命中。

而 [`ShowStep`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L183-L188) 是判决书里 `step` 这一栏的具体内容，只有两个变体：

- **`Recipe(&'a Recipe, RecipeIndex)`**：用户写的转换型 show 规则。携带规则本身 `Recipe` 和一个 `RecipeIndex`（防重入用的「门牌号」）。
- **`Builtin(NativeShowRule)`**：元素的内置 show 规则。

> 关键区分：show-set 规则（`show x: set ...`）**不会**变成 `ShowStep`。`verdict` 在遍历样式链上的 recipe 时，一旦发现某条 recipe 的变换是 `Transformation::Style`，就把它折进 `map` 然后 `continue`，根本不设 `step`。只有 `Content`/`Func` 变换（即真正替换内容的）才有资格成为 `ShowStep::Recipe`。这一点是 4.1.3 源码精读的重点。

#### 4.1.2 核心流程

`verdict` 函数（审理过程）可概括为：

```
verdict(engine, elem, styles) -> Option<Verdict>:
  prepared = elem 是否已 prepared
  map = 空样式集合
  step = None

  # (1) 预合成：若元素未 prepared 且可合成，先 clone 一份跑一次 synthesize，
  #      只为让 figure.where(kind:) 这类「按合成字段匹配」的 selector 能命中。
  if !prepared 且 elem 可合成:
      slot = elem.clone(); slot.synthesize(...); 用 slot 代替 elem 参与 matcher

  depth = 样式链上 recipe 总数（惰性计算）

  for (r, recipe) in 样式链上的 recipes:
      if recipe 的 selector 不匹配 elem: continue

      # (2) show-set 规则：折进 map，不设 step
      if recipe.transform 是 Style:
          if !prepared: map.apply(该样式)
          continue

      # (3) 已经有 step 了就不再找第二条替换型规则
      if step.is_some(): continue

      # (4) 防重入：这条 recipe 若已对 elem 套过（被 guard），跳过
      index = RecipeIndex(depth - r)
      if elem.is_guarded(index): continue

      step = Some(ShowStep::Recipe(recipe, index))
      if prepared: break   # 已 prepared 就不必再继续找 show-set 了

  # (5) 没找到用户规则，再考虑内置 show 规则
  if step.is_none():
      if let Some(rule) = engine.library.rules.get(target, elem):
          step = Some(ShowStep::Builtin(rule))

  # (6) 若既没 step、map 也空、且无需 prepare，则返回 None（表示本元素 show 关卡不认领）
  if step.is_none() && map.is_empty() && 无需 prepare:
      return None

  return Some(Verdict { prepared, map, step })
```

`verdict` 返回 `None` 是 `visit_show_rules` 认领失败的信号（详见 4.3）。注意第 (4) 步的 `RecipeIndex`——它是本模块的防重入核心，4.1.3 单独展开。

#### 4.1.3 源码精读

先看两个数据结构的定义，非常简短：

[src/lib.rs:L172-L180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180) —— `Verdict` 结构体，三个字段 `prepared`/`map`/`step` 分别对应 4.1.1 的三件事。

[src/lib.rs:L183-L188](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L183-L188) —— `ShowStep` 枚举，两个变体 `Recipe`/`Builtin`。

接着看 `verdict` 里**区分 show-set 与转换型规则**的那几行，这是理解 `step` 何时为 `Some` 的关键：

[src/lib.rs:L467-L504](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L467-L504) —— 遍历样式链上的 recipe。注意三段：

- L477–L482：`Transformation::Style` 分支——show-set 规则只折进 `map`（且仅在 `!prepared` 时），随后 `continue`，**不会**设置 `step`。
- L485–L487：已有 `step` 就不再找第二条替换型规则（一个元素每次只套一条用户 recipe）。
- L496：`step = Some(ShowStep::Recipe(recipe, index))`——只有走到这里才算命中一条会替换内容的规则。

现在重点看**防重入**机制。`RecipeIndex` 是一个包裹了 `usize` 的新类型：

[crates/typst-library/src/foundations/styles.rs:L526-L528](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L526-L528) —— `pub struct RecipeIndex(pub usize);`，注释说明它「从样式链顶部」给 recipe 编号。

编号怎么算？在 `verdict` 里：

[src/lib.rs:L490-L493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L490-L493) —— `let index = RecipeIndex(*depth - r);`，其中 `depth` 是链上 recipe 总数、`r` 是从 0 开始的下标。于是链**最顶端**（最先套上的）那条 recipe 拿到最大编号 `depth`，最底端的拿到 `1`。设链上共有 \(d\) 条 recipe、当前是第 \(r\) 条（从 0 起算），则其编号为：

\[ \text{index} = d - r \]

这个编号会被存进元素的一比特集合里作为「门牌号」。具体由 content 上的两个方法完成：

[crates/typst-library/src/foundations/content/mod.rs:L147-L156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L147-L156) —— `is_guarded(index)` 读取编号位是否已置位；`guarded(index)` 在消费 content 时把编号位置位（返回新的 content）。注意 `prepared` 状态也复用同一个位集合的第 0 位：

[crates/typst-library/src/foundations/content/mod.rs:L158-L166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L158-L166) —— `is_prepared`/`mark_prepared` 读写同一个 `lifecycle` 位的第 0 位。

这套「位集合」的真身在 content 的内部元数据里：

[crates/typst-library/src/foundations/content/raw.rs:L83-L87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/raw.rs#L83-L87) —— `lifecycle: SmallBitSet`，注释写明：第 0 位表示 prepared；第 \(n\) 位表示「已被第 \(n\) 条（从 1 计）recipe 套过、勿再套」。

> 把这条链串起来：`verdict` 算出某条 recipe 的 `index` → `is_guarded(index)` 判断它是否已套过 → 若没套过，`step = Some(ShowStep::Recipe(recipe, index))` 把 `index` 一并交出 → `visit_show_rules` 在真正调用 `recipe.apply` 前，用 `output.into_owned().guarded(guard)`（见 4.3.3）把该位置位。于是这条 recipe 产出的新内容若再次流经 `verdict`，对**同一条** recipe 就会 `is_guarded` 命中而跳过，避免无限自递归。这条防线与 show 深度检查（4.4）共同守护递归安全，完整全景见 u3-l6。

> `verdict` 的预合成克隆（L450–L459）、`Selector::matches`、内置规则兜底（L507–L512）与最终 `None` 判定（L515–L527）的细节是下一讲 u2-l3 的主题，本讲只需知道「判决书是怎么来的」。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 推理」的方式，预测两个具体元素各自会拿到怎样的 `Verdict`，建立「判决书」的直觉（无需编译）。

**操作步骤**：

1. 打开 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L530)。
2. 针对下面两个场景，逐步走一遍 4.1.2 的伪代码，填出 `Verdict` 三字段的取值（`prepared`、`map` 是否空、`step` 是 `None`/`Recipe`/`Builtin`）：

   | 场景（Typst 源） | 元素首次到达 show 关卡时的 `prepared` | `map` | `step` | `verdict` 返回 |
   | --- | --- | --- | --- | --- |
   | `= Title`（无任何 show 规则） | （待你填：false） | （待你填） | （待你填：Builtin？） | （待你填：Some） |
   | `#show heading: it => [★#it★]\n= Title` | （待你填） | （待你填） | （待你填：Recipe？） | （待你填） |

3. 再考虑一个 `#show heading: set text(red)` 场景，问自己：它的 `step` 会是 `Some(Recipe)` 吗？`map` 会非空吗？

**需要观察的现象**：

- 第一个场景：没有任何用户规则，但 heading 有内置 show 规则，故 `step = Some(ShowStep::Builtin(..))`，`map` 为空，返回 `Some`。
- 第二个场景：用户写的转换型规则命中，`step = Some(ShowStep::Recipe(..))`，`map` 为空。
- 第三个场景：`Transformation::Style` 被折进 `map`，`step` 仍可能落入 `Builtin`（或 `None`），但**绝不是** `Recipe`。

**预期结果**：你能不依赖运行、仅靠阅读 `verdict` 准确填出上表。这正是「判决书」语义的检验。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `verdict` 要把「替换型规则」和「show-set 规则」分开处理，而不是统一塞进 `step`？

> **参考答案**：因为二者对元素的作用方式完全不同。show-set 只改样式不改内容，可以与「准备」一起提前折进 `map`，最终通过样式链附加；而替换型规则会**产出全新的 content**，必须由 `visit_show_rules` 调用 `recipe.apply` 拿到新内容后再递归。把二者混进同一个 `step` 会让 `visit_show_rules` 的分支逻辑复杂化；分开后，`map` 负责「加样式」、`step` 负责「换内容」，职责清晰。

**练习 2**：`RecipeIndex` 为什么「从链顶往下」编号，而不是用 recipe 在链里的 0 起下标 `r` 直接当编号？

> **参考答案**：因为样式链在 realize 过程中可能向**底部**增长（套上新局部样式），但已套过的规则的相对位置不变。从链顶（最外层、最稳定的端）编号，能让「某条 recipe 的门牌号」在链增长后依然指向同一条规则，保证 `is_guarded` 判定的稳定性。注释（styles.rs L526）与生命周期位注释（raw.rs L85-86）都强调了「from the top」。

---

### 4.2 两条执行路径：Recipe.apply 与 NativeShowRule.apply

#### 4.2.1 概念说明

判决书给了 `step` 之后，`visit_show_rules` 就要**执行**它。执行分两条路：

- **用户规则路径**：`ShowStep::Recipe(recipe, guard)` → 调 [`recipe.apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L489-L511)。`Recipe` 是 Typst 源码里 `show 标的: 转换` 的运行时表示，核心是一个 `Transformation`。
- **内置规则路径**：`ShowStep::Builtin(rule)` → 调 [`rule.apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1122-L1132)。`NativeShowRule` 是原生元素自带的 Rust 函数指针。

[`Recipe`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L447-L461) 持有三样东西：一个可选的 `selector`（`None` 表示无选择子的 `show: rest => ...`，已在样式挂载时处理）、一个 `transform`、一个错误报告用的 `span`。其中 `transform` 的类型 [`Transformation`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L530-L539) 是个三变体枚举：

- `Content(Content)`：`show x: [替换内容]`——直接用一段写死的内容替换。
- `Func(Func)`：`show x: it => ...`——调用用户函数，把元素 `it` 传进去，取返回值。
- `Style(Styles)`：`show x: set ...`——show-set，在 `verdict` 里已被折进 `map`，**不会**走到 `apply` 的替换分支。

[`NativeShowRule`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1093-L1103) 则更底层：它存一个「元素 id + 函数指针」。这个函数指针签名是 `unsafe fn(&Content, &mut Engine, StyleChain) -> SourceResult<Content>`，由每个元素在自己的 `Show` 实现里提供，经 `std::mem::transmute` 做了类型擦除（因为不同元素的 show 函数首参类型不同，靠运行时 `assert_eq!(content.elem(), self.elem)` 保证安全）。

#### 4.2.2 核心流程

两条路径在 `visit_show_rules` 内部是一个 `match step`（详见 4.3.3 的源码）。它们的共同点是：**都返回 `SourceResult<Content>`**（成功则拿到替换后的内容，失败则拿到错误）。差异在于如何拿到这个结果：

```
ShowStep::Recipe(recipe, guard):
  context = Context::new(elem.location(), Some(styles.chain(map)))   # 给用户函数提供上下文
  content = elem.clone().guarded(guard)                              # 打防重入位
  result  = recipe.apply(engine, context, content)
    └─ match recipe.transform:
         Content(c) => c                              # 直接替换
         Func(f)    => f.call(engine, context, [content])?.display()  # 调用户函数
         Style(..)  => (不会到此分支) 

ShowStep::Builtin(rule):
  _scope = TimingScope::new(elem.name())             # 计时
  result  = rule.apply(&elem, engine, styles.chain(map))
              .map(|c| c.spanned(elem.span()))        # 复用原 span
```

两个细节值得记住：

- **用户路径要建 `Context` 并打 guard**：用户函数可能读取 `it.location()` 或 `styles`，所以要构造上下文；同时 `.guarded(guard)` 防止它自递归。
- **内置路径要计时、要复用 span**：内置 show 规则是「热路径」，用 `typst_timing::TimingScope` 单独计时；它产出的是新 content，需要用原元素的 span 重新标注，便于错误定位。

#### 4.2.3 源码精读

先看 `Recipe` 与 `Transformation`：

[crates/typst-library/src/foundations/styles.rs:L447-L461](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L447-L461) —— `Recipe` 结构体。`selector` 为 `None` 的情形注释里点明：那是 `show: rest => ...`（无选择子），已在内容挂载时被「急切应用」，不会进入本讲的逐元素流程。

[crates/typst-library/src/foundations/styles.rs:L530-L539](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L530-L539) —— `Transformation` 三变体：`Content` / `Func` / `Style`。

再看 `Recipe::apply` 的实现，重点看它如何按 `transform` 分发：

[crates/typst-library/src/foundations/styles.rs:L489-L511](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L489-L511) —— `apply`。三件事：

- L495–L506：按 `transform` 分发。`Func` 分支会 `trace` 一个 `Show(content.func().name())` 的追踪点（方便报错时定位到「是在套某元素的 show 规则时出错的」）。
- L499–L502：仅当 `selector.is_some()`（即不是 `show: rest`）时才加 tracepoint。
- L507–L509：若结果 span 是 detached，就用 recipe 自己的 `span` 补上——保证替换内容总能溯源。

注意 `Style` 分支（L505）虽然写在这里，但在本讲的逐元素流程里**走不到**——因为 `verdict` 已经把 `Style` 变换折进了 `map`，不会让它们成为 `ShowStep::Recipe`。

然后看 `NativeShowRule` 与它的 `apply`：

[crates/typst-library/src/foundations/styles.rs:L1093-L1103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1093-L1103) —— `NativeShowRule`：`elem`（元素 id）+ 类型擦除的函数指针 `f`。注释说明了 `transmute` 的安全性依据（`Packed<T>` 是 `Content` 的透明包装）。

[crates/typst-library/src/foundations/styles.rs:L1122-L1132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1122-L1132) —— `apply`：先 `assert_eq!(content.elem(), self.elem)` 确保类型匹配，再 `unsafe` 调用函数指针。

最后回到 `visit_show_rules` 里调用它们的那段（这是本模块实践的插桩位置）：

[src/lib.rs:L375-L394](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L375-L394) —— `match step`。`Recipe` 分支（L379–L386）建 `Context`、`.guarded(guard)`、调 `recipe.apply`；`Builtin` 分支（L389–L393）开 `TimingScope`、调 `rule.apply`、`.spanned(output.span())`。两路结果都汇入 `result`。

#### 4.2.4 代码实践

**实践目标**：在 `ShowStep::Recipe` 与 `ShowStep::Builtin` 两个分支各加一行日志，分别用「用户规则」与「内置规则」触发，亲眼确认两条路径谁被走到、产出什么。

**操作步骤**：

1. 构建 CLI（若前面讲义已构建可跳过）：

   ```bash
   cargo build -p typst-cli
   ```

2. 打开 `crates/typst-realize/src/lib.rs`，在 [L375-L394](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L375-L394) 的两个分支里插桩（示例代码，仅供学习，验证后请还原）：

   ```rust
   ShowStep::Recipe(recipe, guard) => {
       eprintln!("[show] RECIPE 命中 elem={}", output.elem().name());
       let context = Context::new(output.location(), Some(chained));
       recipe.apply(/* ... */)
   }
   ShowStep::Builtin(rule) => {
       eprintln!("[show] BUILTIN 命中 elem={}", output.elem().name());
       let _scope = typst_timing::TimingScope::new(output.elem().name());
       rule.apply(/* ... */)
   }
   ```

3. 准备两个文档。用户规则文档 `user.typ`：

   ```typ
   #show heading: it => [★ #it.body ★]
   = Title
   ```

   仅内置规则文档 `builtin.typ`：

   ```typ
   = Title
   ```

4. 分别运行并把 stderr 存盘：

   ```bash
   cargo run -p typst-cli -- compile user.typ    2> user.log
   cargo run -p typst-cli -- compile builtin.typ 2> builtin.log
   ```

5. 查看日志里 `RECIPE` / `BUILTIN` 的出现情况：

   ```bash
   grep -c 'RECIPE'  user.log
   grep -c 'BUILTIN' builtin.log
   ```

**需要观察的现象**：

- `user.log` 里 heading 元素应触发 `[show] RECIPE 命中 elem=heading`（用户规则优先于内置规则，见 4.1.2 第 (5) 步）。
- `builtin.log` 里 heading 元素应触发 `[show] BUILTIN 命中 elem=heading`（没有用户规则，走内置兜底）。
- 同一个文档里通常两者**不会同时**命中同一个 heading：要么 RECIPE、要么 BUILTIN。

**预期结果**：

- `user.typ` 的 `[show] RECIPE` 计数 ≥ 1，且对 heading 不出现 `[show] BUILTIN`。
- `builtin.typ` 的 `[show] BUILTIN` 计数 ≥ 1。
- **具体计数待本地验证**：一次 compile 会触发多次 realize（flow/inline/pages 各调一次），同一 heading 可能被套多次规则，日志条数会多于你直觉中的「1 条」。

> 进阶：在 `user.typ` 里再加 `#show heading: set text(red)`，观察它**不会**触发 `[show] RECIPE`（show-set 在 `verdict` 里被折进 `map`），从而验证 4.1.1 的关键区分。

#### 4.2.5 小练习与答案

**练习 1**：`Recipe::apply` 里有 `Transformation::Style` 分支，但为什么在本讲的逐元素流程里它「走不到」？

> **参考答案**：因为 `verdict` 在遍历 recipe 时，遇到 `Transformation::Style` 会直接 `map.apply(transform.clone()); continue;`（lib.rs L477-L482），把它折进 `map` 而不设置 `step`。因此能成为 `ShowStep::Recipe` 的 recipe，其 `transform` 只可能是 `Content` 或 `Func`。`apply` 里保留 `Style` 分支是为了其它调用点（如 `show: rest => ...` 的急切应用路径）的完整性。

**练习 2**：内置路径调 `rule.apply` 前为什么不需要像用户路径那样 `.guarded(guard)`？

> **参考答案**：`guard` 的作用是防止**同一条用户 recipe** 在其输出上反复自递归。内置 show 规则是元素自带的、确定性的默认外观函数，不会「匹配自己的输出再套一遍」——它的输出是 well-known 的子元素，由后续 `visit` 正常处理。因此内置路径不需要防重入位，转而需要的是计时与 span 复用。

**练习 3**：`NativeShowRule` 为什么要把函数指针做 `transmute` 类型擦除，而不是用泛型 `NativeShowRule<T>`？

> **参考答案**：因为所有元素的内置 show 规则要被收进**同一个**表（`engine.library.rules`）按 `(target, elem)` 查询，而这个表必须是「类型无关」的容器。不同元素的 show 函数首参类型是 `&Packed<T>`，各 `T` 不同，无法塞进同一个泛型容器。擦除成 `unsafe fn(&Content, ..)` 后统一存储，调用时再靠 `assert_eq!(content.elem(), self.elem)` 恢复类型安全。

---

### 4.3 visit_show_rules 全景：准备、套用、延迟错误、递归

#### 4.3.1 概念说明

4.1 讲了「判决书怎么来」，4.2 讲了「两条执行路径怎么走」。本模块把镜头拉远，看 [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 这个**编排者**如何把它们串成一条完整的处理流水线。它做四件事，顺序不能乱：

1. **拿判决**：调 `verdict`。若返回 `None`，本函数立刻 `return Ok(false)`（show 关卡不认领，交回 `visit` 让后续关卡处理）。
2. **按需准备**：若 `!prepared`，调 [`prepare`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L533-L589)（分配位置、合成字段、生成 tag、`mark_prepared`）。`prepare` 的细节是 u2-l4 的主题，这里只需知道「它只该跑一次，跑完返回 start/end 两个 tag」。
3. **套规则 + 延迟错误**：若有 `step`，按 4.2 执行；执行结果用 `engine.delay` 包裹——**成功取内容、失败暂存错误并退化为空内容**，绝不在此中断编译。
4. **递归**：把（可能被替换的）内容交给 `visit_styled` 重新进入 `visit`。这正是 show 规则的输出能再次匹配其它规则、被分组、被过滤的入口。

`engine.delay` 是本模块的灵魂。它体现了 Typst 的一个重要设计：**realize 处于「内省循环（introspection loop）」之内**，一次 realize 的早期迭代里发生的错误，很可能在后续迭代（布局结果稳定后）就不再发生。所以 show 规则的错误不应一遇到就终止整个编译，而应暂存，等循环结束时若仍存在再升级为致命错误。

#### 4.3.2 核心流程

```
visit_show_rules(s, content, styles) -> SourceResult<bool>:
  # ① 拿判决
  let Some(Verdict { prepared, mut map, step }) = verdict(engine, content, styles)
      else return Ok(false);                      # 不认领

  output = Cow::Borrowed(content)                 # 可变副本

  # ② 按需准备（首访）
  tags = None
  if !prepared:
      tags = prepare(engine, locator, output.to_mut(), &mut map, styles)?   # 返回 (start_tag, end_tag)

  # ③ 套规则（若有 step）+ 延迟错误
  if let Some(step) = step:
      chained = styles.chain(&map)
      result = match step { Recipe(..) => ...; Builtin(..) => ... }   # 见 4.2
      output = Cow::Owned(engine.delay(result))   # 错误→暂存+空内容；成功→内容

  # 生命周期延长（见 u2-l1 的 store / u3-l3）
  realized = match output { Borrowed(c) => c, Owned(c) => s.store(c) }

  # ④ 递归（带 tag、outside、show 深度的「外壳」，细节见 4.4）
  push start_tag（若有）
  visit_styled(s, realized, Cow::Owned(map), styles)?   # 新内容重新喂回流式流水线
  push end_tag（若有）

  return Ok(true)                                 # 认领成功
```

一个关键认知：**`visit_show_rules` 自己不直接把结果 push 进 `sink`**。它把替换后的内容交给 `visit_styled`，后者最终会调 `visit`，由 `visit` 的兜底 `push`（或其它关卡）决定元素落地。这就是「show 规则的输出会再次走完整流水线」的实现方式。

#### 4.3.3 源码精读

先看 ① 拿判决与不认领的早退：

[src/lib.rs:L358-L362](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L358-L362) —— `let Some(Verdict { prepared, mut map, step }) = verdict(... ) else { return Ok(false); };`。注意 `map` 用 `mut` 绑定，因为 `prepare` 还会往里追加内置 show-set 样式。

[src/lib.rs:L365-L372](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L365-L372) —— ② 准备：`output = Cow::Borrowed(content)` 拿一个可写副本；`!prepared` 时调 `prepare`，返回的 `(start, end)` tag 暂存在 `tags`。

接着看 ③ 套规则 + 延迟错误的核心（含 4.2 的 `match step` 与 `engine.delay`）：

[src/lib.rs:L375-L403](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L375-L403) —— `if let Some(step) = step` 整段。L396–L402 的注释把 `delay` 的用意说得很清楚：show 规则里的错误不立即终止编译，而是退化为空内容、把错误攒起来，留到内省循环结束时若仍残留再一并报出——这样既能忽略「只在早期迭代出现」的错误，又能一次性给出更多有用错误。L402 `output = Cow::Owned(s.engine.delay(result));` 就是这一策略的落点。

现在看 `engine.delay` 本体：

[crates/typst-library/src/engine.rs:L38-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L38-L50) —— `delay<T: Default>`：`Ok(v) => v`；`Err(errors) => { 把 errors 塞进 `delayed_errors`；返回 `T::default()` }`。对 `Content` 而言 `default()` 就是空内容。所以一次 show 规则失败 = 「元素临时变空 + 一个延迟错误」，编译继续。

最后看生命周期延长与 ④ 递归：

[src/lib.rs:L405-L409](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L405-L409) —— `realized` 的取得：`Borrowed` 直接用原引用；`Owned`（被替换过的新内容）通过 `s.store`（见 u2-l1）分配进 arena，把生命期延长到 `'a`。

[src/lib.rs:L411-L432](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L411-L432) —— ④ 递归外壳：push start tag → 保存/修改 `outside`、增减 show 深度（详见 4.4）→ `visit_styled(s, realized, Cow::Owned(map), styles)` 把新内容连同 `map` 重新喂回流水线 → 还原 `outside` 与深度 → push end tag → `Ok(true)`。

> 注意 `prepare` 在 4.3.3 里只是被调用，没有展开。它的「分配 location → 内置 show-set → synthesize → materialize → 生成 tag → `mark_prepared`」六步是 u2-l4 的专属内容。本讲只需理解：`prepare` 让元素「只被准备一次」，并产出 start/end tag 供内省系统定位。

#### 4.3.4 代码实践

**实践目标**：用插桩观察「show 规则里的错误被 `engine.delay` 吞成空内容、编译却不中断」的现象。

**操作步骤**：

1. 在 [src/lib.rs:L402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L402) 的 `engine.delay(result)` 处插桩，把 `result` 的成败打出来（示例代码，仅供学习，验证后请还原）：

   ```rust
   let result_for_log = match &result {
       Ok(_) => "Ok".to_string(),
       Err(e) => format!("Err({} 个错误)", e.len()),
   };
   eprintln!("[show] step 执行结果={} elem={}", result_for_log, content.elem().name());
   output = Cow::Owned(s.engine.delay(result));
   ```

2. 准备一个「在 show 规则里故意报错」的文档 `err.typ`：

   ```typ
   #show heading: it => panic
   = 第一条标题
   = 第二条标题
   正文照常排。
   ```

   > `panic` 在 Typst 里会触发一个错误（未定义的标识符），从而让 `recipe.apply` 返回 `Err`。

3. 运行编译：

   ```bash
   cargo run -p typst-cli -- compile err.typ 2> err.log
   echo "退出码: $?"
   ```

4. 观察日志与最终输出：

   ```bash
   grep -c 'step 执行结果=Err' err.log
   ```

**需要观察的现象**：

- `[show] step 执行结果=Err(...)` 会出现（每条 heading 各至少一次），说明 show 规则确实失败了。
- 但编译过程**没有立刻崩溃**：`正文照常排。` 仍被处理，`engine.delay` 让失败的标题退化为空内容后继续。
- 编译结束时，Typst 会在汇总阶段把延迟错误报给用户（stderr 里能看到错误信息）。

**预期结果**：

- `step 执行结果=Err` 计数 ≥ 1。
- 退出码非 0（因为延迟错误最终升级为致命错误），但**不是在套规则的瞬间**就退出——这正是「延迟」的含义。**精确的退出时机与错误条数待本地验证**。

> 对照实验：若你临时把 `engine.delay(result)` 改成 `result?`（即立即向上传播错误），会发现编译在**第一条**失败标题处就中断，后续内容不再处理。这能直观对比「延迟」与「立即」两种错误策略。

#### 4.3.5 小练习与答案

**练习 1**：`visit_show_rules` 为什么不直接把 `realized` push 进 `sink`，而要走 `visit_styled`？

> **参考答案**：因为 show 规则的输出**不是成品**——它可能再次匹配其它 show 规则、需要被分组（如收成段落）、需要被过滤，甚至携带新的局部样式（`map`）。走 `visit_styled(... realized, map, styles)` 能把 `map` 作为局部样式挂上、再让 `visit` 对 `realized` 跑完整 8 步流水线。直接 push 会跳过所有后续处理，破坏具现化的「递归套用到收敛」语义。

**练习 2**：`engine.delay` 把失败退化为「空内容」。如果一个 heading 的 show 规则永远失败，这个标题最终会怎样？

> **参考答案**：它在 realize 阶段变成空内容（占位消失），排版结果里该标题位置为空；同时一条延迟错误被记录。若该错误在内省循环的所有迭代里都存在（即不是「早期迭代特有的瞬时错误」），它会在循环结束时升级为致命错误报给用户。也就是说：用户会看到错误，但编译过程能尽量多跑、多收集错误，而不是在第一个错误处卡死。

**练习 3**：`Verdict` 里的 `map` 为什么用 `mut` 绑定，并且在 `prepare` 里还会被追加？

> **参考答案**：`verdict` 收集的 `map` 只包含**用户** show-set 规则折进来的样式；而元素自己还可能有**内置** show-set（`ShowSet` trait，在 `prepare` 里通过 `show_settable.show_set(styles)` 取得，lib.rs L560-L562）。后者必须等元素准备好才能算，所以只能在 `prepare` 里追加进同一个 `map`。用 `mut` 绑定正是为了让 `prepare` 能就地扩充它。

---

### 4.4 show 输出的外壳：tag、outside 与 show 深度

#### 4.4.1 概念说明

4.3 的 ④ 里，`visit_styled` 那一行前后还夹着三组操作：push start tag、动 `outside` 与 show 深度、push end tag。它们共同构成 show 输出的「外壳」——包裹在递归外层、保证内省、页面样式提升、递归安全这三件事不被破坏。

- **start/end tag**：`prepare` 给可定位元素（locatable、带 label、或带 location）生成的一对 `Tag`。它们是内省系统（`query`、`locate`）在排版后的帧里「找回这个元素」的锚点。start tag 必须排在元素内容之前、end tag 排在之后，所以 `visit_show_rules` 在 `visit_styled` 前后各 push 一个。
- **`outside` 的入笼/出笼**：u2-l1 已铺垫——进入一个元素的 show 输出时，`outside` 要被「压低」（仅当元素是 `ContextElem` 才保持 true），返回时恢复。这保证「show 规则产出里的页面样式」不会被错误地提升到 page 层。
- **show 深度**：每进入一次 show 规则的递归，`engine.route` 的深度 +1，并检查是否超过 `MAX_SHOW_RULE_DEPTH = 64`。这是防止 show 规则无限自递归（即使有 4.1 的 guard，理论上仍可能在不同规则间循环）的最终防线。

#### 4.4.2 核心流程

```
# ④ 递归外壳（lib.rs L411-L430）
let (start, end) = tags.unzip();
if let Some(tag) = start { visit(s, store(TagElem::packed(tag)), styles)? }   # push start tag

prev_outside = s.outside
s.outside &= content.is::<ContextElem>()          # 入笼：show 输出默认「不在外层」
s.engine.route.increase()                          # show 深度 +1
s.engine.route.check_show_depth().at(content.span())?   # 超过 64 → 报 "maximum show rule depth exceeded"
visit_styled(s, realized, Cow::Owned(map), styles)?
s.engine.route.decrease()                          # show 深度 -1
s.outside = prev_outside                           # 出笼：还原

if let Some(tag) = end { visit(s, store(TagElem::packed(tag)), styles)? }     # push end tag
```

三件事各自的意义：

- **tag**：成对出现，把元素内容「夹」在中间。tag 本身是 `TagElem`，在 `visit` 的第 1 步（u1-l3）被**直推**进 sink，不做任何转换。
- **outside**：典型的「保存—修改—恢复」栈式纪律，保证 show 规则的副作用不污染外层状态。
- **show 深度**：`increase`/`check`/`decrease` 三连，对应一次 show 递归的进入与退出；`check_show_depth` 一旦失败，整个 `realize`（乃至编译）以错误终止。

#### 4.4.3 源码精读

先看 tag 的 push 与 `outside`/深度的「夹逼」结构：

[src/lib.rs:L411-L430](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L411-L430) —— 完整的 ④ 外壳。逐行：

- L412 `let (start, end) = tags.unzip();`：`tags` 是 `Option<(Tag, Tag)>`，`unzip()` 拆成两个 `Option<Tag>`。
- L413–L415：start tag 经 `TagElem::packed` 包装后 `visit`（被第 1 步直推进 sink）。
- L417–L418：保存 `prev_outside`，再 `s.outside &= content.is::<ContextElem>()`。`ContextElem` 是例外——它代表「透传上下文」，其 show 输出逻辑上仍在外层。
- L419–L420：`route.increase()` 后立即 `check_show_depth().at(content.span())?`——超深则把错误挂在元素 span 上抛出。
- L422：核心递归 `visit_styled`。
- L424–L425：`route.decrease()` + 还原 `outside`。
- L428–L430：end tag 同样直推。

再看 `route` 的深度机制：

[crates/typst-library/src/engine.rs:L325-L333](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L325-L333) —— `increase`/`decrease`：`route` 的某段深度计数器 `len` 的 +1/-1。

[crates/typst-library/src/engine.rs:L342-L363](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L342-L363) —— `MAX_SHOW_RULE_DEPTH = 64` 与 `check_show_depth`：超过即 `bail!("maximum show rule depth exceeded")`，并给出两条提示（「可能某条 show 规则匹配了自己的输出」「可能元素嵌套过深」）。注释（L336-L339）还解释了为何各类深度上限取值不同：**深度上限越低者优先级越高**，这样 show 规则问题与函数调用问题交错时，总能优先报 show 规则错误。

> 关于 `outside` 在 `visit_styled` 里的另一处使用（页面样式提升），u2-l1 的 4.3 与 u2-l5 已详述，本讲只关注它在 show 递归前后的「入笼/出笼」这一面。

#### 4.4.4 代码实践

**实践目标**：插桩观察 show 递归深度的增减，并亲手触发一次 `maximum show rule depth exceeded`，验证 64 这道防线。

**操作步骤**：

1. 在 [src/lib.rs:L419-L420](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L419-L420) 的 `increase` 与 `check` 之间插桩（示例代码，仅供学习，验证后请还原）：

   ```rust
   s.engine.route.increase();
   eprintln!("[depth] 进入 show 递归，当前深度+1");
   s.engine.route.check_show_depth().at(content.span())?;
   ```

2. 准备一个**自递归**的 show 规则文档 `loop.typ`（让 heading 的 show 输出里又包含 heading，从而绕过单条 recipe 的 guard、形成跨规则的循环）：

   ```typ
   #let me = heading
   #show heading: it => me
   = boom
   ```

   > 这里用 `#let me = heading` 重新绑定，使 show 输出产生一个「新的」heading，试图绕过 `RecipeIndex` 的单规则 guard，把递归推到深度上限。

3. 运行编译：

   ```bash
   cargo run -p typst-cli -- compile loop.typ 2> loop.log
   echo "退出码: $?"
   tail -n 20 loop.log
   ```

4. 统计 `[depth]` 日志条数：

   ```bash
   grep -c '\[depth\]' loop.log
   ```

**需要观察的现象**：

- `[depth]` 日志会连续出现很多条（每多一层 show 递归一条），直到 `check_show_depth` 触发。
- 最终 stderr 里出现 `maximum show rule depth exceeded` 及其提示。
- 退出码非 0。

**预期结果**：

- `[depth]` 计数会攀升到一个与 `MAX_SHOW_RULE_DEPTH = 64` 相关的上限后停止增长（精确数值取决于 route 在进入 realize 前已有的基础深度，**待本地验证**）。
- 报错信息与 [engine.rs:L356-L360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L356-L360) 的文案一致。

> 对照实验：把 `loop.typ` 换成正常的 `#show heading: it => [★#it★]\n= ok`，`[depth]` 日志应寥寥几条（每个 heading 套一次规则、深度只短暂 +1 后立刻 -1），不会触顶。

#### 4.4.5 小练习与答案

**练习 1**：start tag 和 end tag 为什么必须分别排在 `visit_styled` 的**前后**，而不是一起 push？

> **参考答案**：因为 tag 是内省系统在排版帧里定位元素的锚点——start tag 标记「元素内容的起点」、end tag 标记「终点」。只有把它们分别置于内容两侧，`query`/`locate` 才能在最终的帧序列里圈出这个元素实际占据的区域。若一起 push，锚点就失去「夹住内容」的语义。

**练习 2**：`s.outside &= content.is::<ContextElem>()` 这一句，为什么用 `&=`（与赋值）而不是直接 `s.outside = false`？

> **参考答案**：因为外层可能本来就 `outside = false`（比如已经在某个容器里），此时无论元素是什么都不该变 true；`&=` 保留了「外层原本不在外层」这一信息，只在「外层确实在外层 + 当前元素是 ContextElem」时才维持 true。`ContextElem` 代表透传上下文，它的 show 输出逻辑上仍属于外层，故作为例外保留 true。直接赋 `false` 会丢失外层状态。

**练习 3**：`check_show_depth` 的上限是 64，而 `RecipeIndex` 的 guard 已经能防「同一条 recipe 自递归」。为什么还需要这道深度防线？

> **参考答案**：`guard` 只防**同一条** recipe 在自己的直接输出上重复套用；但 show 规则之间可以形成**跨规则的循环**（规则 A 的输出匹配规则 B，B 的输出又匹配 A），或者一条规则经分组/准备后间接地产出能再次匹配自己的内容。这类循环绕过了单规则 guard，只能靠「整体递归深度」这道粗粒度防线兜底。两道防线的关系与典型触发场景，u3-l6 会给出完整全景。

## 5. 综合实践

把本讲四块（判决书、两条执行路径、编排者、递归外壳）串起来，做一次「画调用图 + 插桩追踪 + 触发边界」的综合练习。

**任务**：为 `visit_show_rules` 画一张完整的内部调用图，并用一个含用户 show 规则的文档，把「判决 → 准备 → 套用 → 延迟错误 → 带 tag/深度递归」这一整条链路用日志串起来。

**操作步骤**：

1. **画调用图**。在纸上或文档里画出 `visit_show_rules` 的内部结构，标出它调用的函数与它们所在的讲义：

   ```
   visit_show_rules
     ├─ verdict .................. (本讲 4.1；细节 u2-l3)
     │    └─ Selector::matches, engine.library.rules.get
     ├─ prepare .................. (本讲 4.3 调用；细节 u2-l4)
     │    └─ mark_prepared, Synthesize, ShowSet, 生成 Tag
     ├─ (match step)
     │    ├─ ShowStep::Recipe  → Recipe::apply ......... (本讲 4.2)
     │    └─ ShowStep::Builtin → NativeShowRule::apply . (本讲 4.2)
     ├─ engine.delay ............ (本讲 4.3)
     ├─ s.store (生命周期延长) ... (u2-l1 / u3-l3)
     ├─ visit(start_tag) ........ (直推，u1-l3)
     ├─ outside 入笼 / route.increase / check_show_depth  (本讲 4.4)
     ├─ visit_styled(realized, map) → visit ............ (新内容回流，u2-l5)
     ├─ route.decrease / outside 出笼 .................. (本讲 4.4)
     └─ visit(end_tag) .......... (直推，u1-l3)
   ```

   把每条边上标注「在哪一行」（参考本讲各源码精读的行号链接）。

2. **插桩追踪**。把 4.2.4、4.3.4、4.4.4 的三处插桩**同时**打开，准备一个综合文档 `mix.typ`：

   ```typ
   #show heading: it => [★ #it.body ★]
   #show heading: set text(red)

   = 第一条
   正文。
   = 第二条
   ```

3. **运行并串读日志**：

   ```bash
   cargo run -p typst-cli -- compile mix.typ 2> mix.log
   grep -E '\[show\]|\[depth\]|step 执行结果' mix.log | head -n 40
   ```

4. **对照调用图解读**：对每一条 heading，日志应大致呈现 `[show] RECIPE/BUILTIN 命中` → `[depth] 进入 show 递归` → （若有错误）`step 执行结果=...` 的序列。确认它与你画的调用图一致。

**预期结果**（定性，**精确序列与计数待本地验证**）：

- `#show heading: set text(red)` 这条 show-set 规则**不**触发 `[show] RECIPE`，印证 4.1 的关键区分。
- 每条 heading 的转换型规则各触发一次 `[show] RECIPE 命中 elem=heading`，紧随其后是 `[depth]`。
- `step 执行结果=Ok`（因为没有错误），`engine.delay` 走 `Ok` 分支。
- 你能指着日志的每一行，说清它对应调用图的哪条边——这就达成了「把 show 规则应用流程读通」的目标。

## 6. 本讲小结

- [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 是 `visit` 流水线的第 3 道关卡，负责「给元素套 show 规则并准备好」。它先向 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L530) 索取一份 [`Verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180) 判决书（`prepared`/`map`/`step`），判决为 `None` 就 `Ok(false)` 不认领。
- [`ShowStep`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L183-L188) 有两路：`Recipe`（用户转换型规则）与 `Builtin`（内置规则）。**show-set 规则（`Transformation::Style`）不变成 `ShowStep`**，而是在 `verdict` 里被折进 `map`。
- 两条执行路径分别是 [`Recipe::apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L489-L511)（建 `Context`、`.guarded(guard)` 防重入、按 `Content`/`Func` 变换分发）与 [`NativeShowRule::apply`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1122-L1132)（计时、类型擦除函数指针、复用 span）。
- show 规则的错误经 [`engine.delay`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L38-L50) 暂存为延迟错误、内容退化为空，**不立即终止编译**——这是为内省循环里「只在早期迭代出现的瞬时错误」留余地。
- show 输出套着一层「外壳」：start/end [`Tag`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L411-L430) 供内省定位、`outside` 入笼/出笼保护页面样式提升、`route.increase`/`check_show_depth`（上限 [`MAX_SHOW_RULE_DEPTH = 64`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L342-L363)）防递归失控。
- `RecipeIndex` 的防重入位（[`is_guarded`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L147-L156)/[`guarded`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L153-L156)）与 show 深度防线共同守护递归安全，前者防单规则自递归、后者防跨规则循环。

## 7. 下一步学习建议

本讲把 `visit_show_rules` 的骨架与两条执行路径讲清了，但有几处刻意留白，正好是后续讲义的主题：

- **u2-l3（verdict——决定如何处理元素）**：本讲的判决书是 `verdict` 现场开出来的，但我们只用了它的结论。下一讲深入 `verdict` 的预合成克隆（为什么 `figure.where(kind:)` 必须先 clone 再 synthesize）、`Selector::matches`、内置规则兜底与最终 `None` 判定。
- **u2-l4（prepare——元素的首次准备）**：本讲里 `prepare` 只是被调用。下一讲拆它的「分配 location → 内置 show-set → synthesize → materialize → 生成 tag → `mark_prepared`」六步，理解「只准备一次」的幂等性。
- **u2-l5（visit_styled 与页面/文档样式）**：本讲 ④ 把替换内容交给了 `visit_styled`。下一讲讲它如何处理 DocumentElem/TextElem/PageElem 样式、locale 推断与 pagebreak「突破笼子」，把 `outside` 的另一半讲完。
- **u3-l1（标签与内省 TagElem）**：本讲的 start/end tag 只是 push 进 sink；它们如何跨分组边界被纳入/排除，是内省专题的内容。
- **u3-l6（递归深度限制与错误处理）**：本讲点到的 `RecipeIndex` guard 与 `check_show_depth`、以及分组侧的 512 上限，在那里有完整的「防循环」全景。

阅读源码时，建议把本讲 4.3.3 的「① ② ③ ④」四步骨架常备手边：每读到一个 show 规则相关的问题，先定位它属于「判决 / 准备 / 套用与延迟 / 递归外壳」的哪一段，再深入对应函数，就不容易在 `visit_show_rules` 里迷路。
