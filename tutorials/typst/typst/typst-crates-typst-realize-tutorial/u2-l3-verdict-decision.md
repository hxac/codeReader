# verdict——决定如何处理元素

## 1. 本讲目标

上一讲（u2-l2）我们跟着 [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 走完了「套用 show 规则」的全过程，但在它的第一行就遇到了一个被原样调用、却没展开的黑盒：

```rust
let Some(Verdict { prepared, mut map, step }) = verdict(s.engine, content, styles)
else {
    return Ok(false);
};
```

这个 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L529) 正是本讲的主角。它是 show 规则流水线的「预审法官」：在真正替换任何内容之前，先审一审当前元素——它要不要被准备？要不要附加 show-set 样式？有没有命中某条用户 show 规则？要不要回退到内置 show 规则？审完产出一份 [`Verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180)「判决书」，`visit_show_rules` 只需照着判决执行。

学完本讲，你应该能够：

- 说清 `verdict` 的输入、输出与五步审理流程，并解释它在什么情况下返回 `None`（让 `visit_show_rules` 直接放弃认领）。
- **理解为什么 `verdict` 要先 `clone` 再「预合成」**：这是为了让 `show figure.where(kind: table)` 这类「按合成字段筛选」的规则能命中。
- 区分 **show-set 规则**（`show x: set ...`，折进 `map`、不替换内容）与 **transformational show 规则**（替换内容、产出 `ShowStep`）两条分流。
- 掌握 [`RecipeIndex`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L526-L528) / [`is_guarded`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L147-L150) 这套「从链顶编号 + 生命周期位集」的防重入机制是如何防止 show 规则无限自递归的。

本讲是 u2-l2 的向下钻取，也是 u2-l4 `prepare` 的铺垫——`Verdict.prepared` 这个布尔正是交给 `prepare` 的「要不要再准备一遍」的开关。

## 2. 前置知识

本讲承接 u1-l3、u2-l1、u2-l2，假定你已经了解：

- **`visit()` 8 步流水线与短路语义**（u1-l3）：`visit_show_rules` 是第 3 步，返回 `false` 表示「我不认领，交给后续步骤」。
- **`State` 工作台与 `Verdict`/`ShowStep` 结构**（u2-l1、u2-l2）：判决书有三栏 `prepared`/`map`/`step`，`step` 是 `Option<ShowStep>`，`ShowStep` 分 `Recipe`（用户）与 `Builtin`（内置）两路。
- **`StyleChain` 与 `Recipe`**（u1-l2、u2-l2）：样式链是只读叠层的「链表」，用户写的每条 show 规则就是一个 `Recipe`，挂在链上；`styles.recipes()` 按「内层 → 外层」顺序遍历它们。

本讲新引入几个概念，先通俗解释：

- **预合成（pre-synthesis）**：某些元素（如 `figure`）的字段不是用户直接填的，而是由其它字段「推导」出来的（例如 `figure.kind` 由它的 body 是图片还是表格推导）。这种推导叫合成（synthesize）。`verdict` 为了能在「匹配 selector」阶段就读到这些推导字段，会先在一个**克隆体**上提前跑一次合成，这就是预合成。
- **show-set 规则**：写作 `show heading: set text(red)`。它不改变内容，只给匹配的元素**附加样式**。`verdict` 把它收进 `map`，而不是当成会替换内容的 `ShowStep`。
- **transformational show 规则**：写作 `show heading: it => [★ #it.body ★]`，它会**用新内容替换**旧元素。`verdict` 把它记录成 `ShowStep::Recipe`。
- **防重入（guard）**：一条 show 规则替换出的内容，可能再次匹配同一条规则，从而无限套娃。`verdict` 用一个「这条规则已经套过」的标记位来跳过重复套用，这就是防重入。
- **生命周期位集（lifecycle）**：每个 `Content` 内部带一个 `SmallBitSet`，用不同的 bit 位同时承载「是否已 prepared（位 0）」和「哪些 RecipeIndex 已套过（位 ≥1）」两类信息，是一种紧凑的标记复用。

## 3. 本讲源码地图

本讲在三个文件之间穿梭：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | 本讲主角 `verdict`、`Verdict`/`ShowStep` 结构都在这里。 |
| [crates/typst-library/src/foundations/styles.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs) | `Recipe`/`Transformation`/`RecipeIndex` 定义、`StyleChain::recipes` 遍历、内置规则的 `NativeRuleMap::get` 查找。 |
| [crates/typst-library/src/foundations/selector.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs) | `Selector` 枚举与命中判定 `Selector::matches`。 |
| [crates/typst-library/src/foundations/content/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs) | `is_guarded`/`guarded`/`is_prepared`/`mark_prepared`——防重入与 prepared 都落在这里。 |
| [crates/typst-library/src/foundations/content/element.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs) | `Synthesize` trait（预合成要调的接口）与 `ShowSet` trait。 |

> 链接基准 HEAD：`32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。下面所有永久链接均基于此 HEAD。

## 4. 核心概念与源码讲解

### 4.1 verdict 函数：从元素到判决书

#### 4.1.1 概念说明

`verdict` 是 show 规则流水线里唯一的「预审」环节。它的职责不是「执行替换」，而是「判定该不该执行、怎么执行」，把结论装进 [`Verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L172-L180) 交还。这样 `visit_show_rules` 的执行逻辑就能保持干净：只负责「照判决执行」。

`Verdict` 三栏的含义：

- **`prepared: bool`**：元素之前是否已被 `prepare` 过。判决书把它原样带回来，让 `visit_show_rules` 决定「这次还要不要再 prepare 一遍」。
- **`map: Styles`**：要附加到元素上的样式集合（主要来自 show-set 规则与内置 `ShowSet`）。注意这是「只改样式、不换内容」的部分。
- **`step: Option<ShowStep>`**：若为 `Some`，表示找到了一条会**替换内容**的 show 规则（`ShowStep::Recipe` 是用户规则、`ShowStep::Builtin` 是内置规则）；若为 `None`，表示没有任何替换型规则命中。

`verdict` 有一个极重要的返回约定：**当「没有任何事可做」时返回 `None`**。此时 `visit_show_rules` 会 `return Ok(false)`——也就是「show 规则不认领这个元素」，把它交还给 `visit` 的后续步骤（序列递归、分组、过滤、兜底 push）。所以 `None` 不是错误，而是一种「这元素不归我管」的信号。

#### 4.1.2 核心流程

`verdict` 的审理可以分为五步：

```text
verdict(engine, elem, styles)
  │
  ├─ 0. 读 prepared = elem.is_prepared()，初始化空 map、空 step
  │
  ├─ 1. 【预合成】若 !prepared 且元素可合成：
  │      克隆一份 → 在克隆体上跑 synthesize → 用克隆体参与后续匹配
  │      （仅为让 selector 能读到合成字段，见 4.2）
  │
  ├─ 2. 【遍历 recipes】对 styles.recipes() 里每条 recipe：
  │      a. selector 不匹配 → 跳过
  │      b. 是 show-set（Transformation::Style）→ 折进 map，continue
  │      c. 已有 step → continue（只收第一条替换型规则）
  │      d. 计算 RecipeIndex，若 is_guarded → continue（防重入，见 4.4）
  │      e. 记录 step = ShowStep::Recipe(recipe, index)
  │      f. 若已 prepared → break（无需再继续找 show-set）
  │
  ├─ 3. 【内置兜底】若 step 仍为 None：
  │      用当前 target 在 NativeRuleMap 里查内置 show 规则 → ShowStep::Builtin
  │
  ├─ 4. 【早退判定】若 step 与 map 都空、且（已 prepared 或元素「无任何需准备的特征」）：
  │      return None  ← 信号：「show 规则不认领」
  │
  └─ return Some(Verdict { prepared, map, step })
```

注意步骤 2 的一个微妙设计：**替换型规则只取第一条**（一旦 `step` 有值，后续 recipe 的 selector 即便匹配也只可能贡献 show-set，不会再覆盖 `step`）。这与 Typst「最内层的 show 规则先生效」的语义一致——`styles.recipes()` 是从内层到外层遍历的。

#### 4.1.3 源码精读

先看函数签名与初始化：

[crates/typst-realize/src/lib.rs:L437-L446](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L446) —— `verdict` 的入参是「引擎、元素、样式链」，返回 `Option<Verdict>`。开篇三行先把判决书的三栏初值备好：`prepared` 从元素身上读，`map`/`step` 先置空。

步骤 3 的内置兜底，逻辑很短却很关键：

[crates/typst-realize/src/lib.rs:L506-L512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L506-L512) —— 只有当**没有任何用户规则**命中时，才去查内置规则。`target` 取自 `styles.get(TargetElem::target)`，它标识当前是给 Paged / Html / Bundle 哪种后端做具现化（不同后端的同一元素可能有不同内置外观）。`engine.library.rules.get(target, elem)` 按 `(元素函数, target)` 在 [`NativeRuleMap`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L1065-L1069) 里查到对应的 `NativeShowRule`。

最反直觉的是步骤 4 的早退判定：

[crates/typst-realize/src/lib.rs:L513-L528](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L513-L528) —— 返回 `None` 的条件是「三无」：`step` 空、`map` 空、且元素「不需要被 prepare」。这里 `prepared || { ... }` 的意思是：要么它已经 prepared 过（那自然不用再 prepare），要么它**没有任何**需要 prepare 才能获得的属性——没有 label、没有 location、不带 `ShowSet`、不可定位、不带 tag、不可合成。只要这六个条件全满足，`prepare` 对它就是空操作，于是整个 `verdict` 毫无可做之事，返回 `None` 让 `visit_show_rules` 放弃认领。

> 把这一段和 u2-l2 联起来看：`verdict` 返回 `None` ⟹ `visit_show_rules` 返回 `Ok(false)` ⟹ `visit` 继续往下走序列/样式/分组/过滤/兜底 push。所以一个「干净」的元素（比如一个已经 prepared 过、又没匹配任何 show 规则的 `TextElem`）会直接被 `verdict` 放行，省掉一次 `prepare` 的重复检查。

#### 4.1.4 代码实践

**实践目标**：直观验证「`verdict` 返回 `None` 时 show 规则不认领元素」，并统计一次 realize 里有多少元素被 `verdict` 判为 `None`。

**操作步骤**：

1. 打开 `crates/typst-realize/src/lib.rs`，在 `verdict` 函数体最开头（约 L444 之后）加入：
   ```rust
   eprintln!("[verdict] enter elem={:?}", elem.func().name());
   ```
2. 在 `return None;`（L526）之前加入：
   ```rust
   eprintln!("[verdict] => None (not claimed) elem={:?}", elem.func().name());
   ```
3. 在 `Some(Verdict { prepared, map, step })`（L528）之前加入：
   ```rust
   eprintln!("[verdict] => Some prepared={} has_step={} map_empty={}", prepared, step.is_some(), map.is_empty());
   ```
4. 用一个最小文档触发编译，例如：
   ```typst
   #set text(size: 12pt)
   = 标题
   一段普通文本 #highlight[带高亮]。
   #show heading: it => [★ #it.body]
   = 又一个标题
   ```

**需要观察的现象**：日志里会看到大量 `[verdict] => None`，尤其是 `TextElem`、`SpaceElem` 这类「干净」元素；而 `heading` 在第一条 `= 标题`（此时 show 规则尚未出现在样式链中）与第二条（show 规则已生效）会呈现不同的判决结果。

**预期结果**：第二条标题命中用户 show 规则，`verdict` 返回 `Some` 且 `has_step=true`；普通文本元素大多返回 `None`（无 step、无 map、无可准备属性）。具体输出依赖编译，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个元素反复 `mark_prepared`，`verdict` 的早退判定会更倾向于返回 `None` 还是 `Some`？为什么？

> **参考答案**：更倾向于返回 `Some`（或至少跳过重复 prepare）。因为早退条件里有 `prepared || { ... }`，prepared 为真时直接满足「无需准备」前提；但早退还要求 `step` 与 `map` 同时为空。换句话说，已 prepared 只是把「需要 prepare」这一项排除了，若仍命中了 show 规则（`step` 非空）则仍返回 `Some`，只是 `visit_show_rules` 会因为 `prepared` 为真而跳过 `prepare` 调用。

**练习 2**：为什么内置 show 规则的查找放在「遍历 recipes 之后」、且仅在 `step.is_none()` 时才查？

> **参考答案**：因为用户规则优先级高于内置规则——只要有一条用户 transformational 规则命中，就用用户的，不再回退内置。内置规则是「没有用户规则时的默认外观」，属于兜底。把它放在循环之后并用 `step.is_none()` 守卫，恰好实现了「用户优先、内置兜底」的语义。

---

### 4.2 预合成克隆：让 figure.where(kind: table) 能命中

#### 4.2.1 概念说明

这是 `verdict` 里最「无奈」的一段代码，源码注释本身就带着哭脸：

> Do pre-synthesis on a cloned element to be able to match on synthesized fields before real synthesis runs (during preparation). It's really unfortunate that we have to do this, but otherwise `show figure.where(kind: table)` won't work :(

问题背景是这样的。`figure` 元素有一个 `kind` 字段，但用户写 `figure` 时通常**不直接填** `kind`——它是由 `figure` 的 body 内容推导出来的：body 是图片就推导为 `image`，是表格就推导为 `table`，等等。这种「由其它字段推导出的字段」就是合成字段（synthesized field），推导过程由 [`Synthesize`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L265-L271) trait 的 `synthesize` 方法完成。

麻烦在于：`verdict` 在**步骤 2 匹配 selector 之前**就需要知道 `kind`，否则 `figure.where(kind: table)`（本质是一个带字典条件的 `Selector::Elem`）无法匹配；而真正「正式」的合成要到稍后 `prepare` 阶段才发生（见 u2-l4）。时间顺序对不上。

解决办法是：**在匹配之前，先克隆一份元素，在克隆体上提前跑一次 synthesize**，让匹配用的 `elem` 引用指向这份「已经补全了合成字段」的克隆体。注意这只为「匹配 selector」服务，正式合成稍后仍会在 `prepare` 里再跑一遍——所以才说「很遗憾不得不做两次」。

#### 4.2.2 核心流程

```text
若 !prepared 且 elem 实现了 Synthesize：
    slot = elem.clone()                      // 克隆一份（不污染原元素）
    slot.synthesize(engine, styles).ok()     // 在克隆体上预合成，错误忽略
    elem = &slot                             // 后续 selector 匹配用克隆体
否则：
    elem 维持指向原元素
```

两个关键点：

1. **只在 `!prepared` 时做**：元素一旦 prepared，说明它已经正式合成过了，字段已经齐备，无需再预合成。
2. **错误被 `.ok()` 丢弃**：预合成只是为了「尽量让 selector 能匹配」，合成失败也不该阻止后续流程（正式合成时还会再报错）。

#### 4.2.3 源码精读

[crates/typst-realize/src/lib.rs:L448-L461](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L448-L461) —— 注意这里用了一个 Rust 借借绕的小技巧：`let mut elem = elem;` 先把外层参数 `elem`（类型 `&'a Content`）「遮蔽」成本地可变绑定，`let mut slot;` 在 `if` 外预先声明，这样 `slot` 的生命周期就能覆盖到 `if` 之后的代码；当进入 `if` 分支时，`elem = &slot` 让 `elem` 指向本地克隆体。后续整段 recipe 匹配（步骤 2）拿到的都是这份带合成字段的克隆体。

[crates/typst-library/src/foundations/content/element.rs:L265-L271](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L265-L271) —— `Synthesize::synthesize(&mut self, engine, styles)` 会就地修改元素，补全它的合成字段。`verdict` 正是借它来「提前补全」。

> 为什么不直接在原元素上合成？因为 `verdict` 拿到的是 `&'a Content`（不可变借用），而且正式合成属于 `prepare` 的职责——在预审阶段就改原元素会破坏「prepare 只跑一次」的幂等性约定。克隆一份是唯一不污染原元素的做法。

#### 4.2.4 代码实践

**实践目标**：用一个 `show figure.where(kind: table)` 文档，结合预合成逻辑，观察元素在 `clone + synthesize` 前后关键字段的变化，理解「为什么必须先克隆再合成」。

**操作步骤**：

1. 在 `verdict` 的预合成分支内（约 L455 之后、`elem = &slot;` 之前）插入日志，对比克隆体合成前后的 `kind` 字段：
   ```rust
   eprintln!("[pre-synth] before: kind={:?}",
       slot.get(FigureElem::data().kind, Some(styles)).ok());
   slot.with_mut::<dyn Synthesize>().unwrap().synthesize(engine, styles).ok();
   eprintln!("[pre-synth] after:  kind={:?}",
       slot.get(FigureElem::data().kind, Some(styles)).ok());
   ```
   > 说明：`FigureElem::data().kind` 是访问 `figure.kind` 字段的字段句柄写法；若你的本地版本字段访问 API 略有差异，可改用 `slot.field("kind")` 之类的等价调用。**字段句柄的确切写法待本地确认**。
2. 用以下文档触发编译：
   ```typst
   #show figure.where(kind: table): it => [表格插图 →] + it.body

   #figure(table(columns: 2, [A], [B]))
   #figure(image("glacier.jpg"))
   ```

**需要观察的现象**：

- 对 `table` 那个 figure：预合成前 `kind` 可能是缺省/`auto`，预合成后被推导为 `table`，于是 selector `figure.where(kind: table)` 匹配成功，该 figure 命中 show 规则。
- 对 `image` 那个 figure：预合成后 `kind` 为 `image`，不匹配 `kind: table`，走内置规则。

**预期结果**：只有表格 figure 被 `[表格插图 →]` 前缀替换；图片 figure 不受影响。若去掉预合成（注释掉 L454-L461），则两条 figure 的 `kind` 在匹配时都还没推导出来，`figure.where(kind: table)` 将**全部匹配不到**——这正是注释里那句哭脸的由来。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：预合成既然会丢弃错误（`.ok()`），那真正的合成错误会在哪里被报出来？

> **参考答案**：在 `prepare` 阶段。`prepare` 会再次调用 `synthesize`（正式合成，详见 u2-l4），那一次的错误会被正常传播（`?`），从而进入编译诊断。预合成的 `.ok()` 只是「尽力而为」地让 selector 能匹配，不承担报错职责。

**练习 2**：假如某元素的合成非常昂贵（比如要跑一次 query），`verdict` 的预合成会不会造成明显的性能浪费？能想到什么缓解思路吗？

> **参考答案**：会。因为同一次 realize 里，合成被跑了两次（预合成一次、`prepare` 里正式一次）。源码作者已经用注释承认这是「unfortunate」。缓解思路包括：给合成结果做 memo 缓存、或把预合成限制在「selector 确实需要读合成字段」时才触发。不过当前实现选择「简单正确优先」，宁可多跑一次也要保证 `figure.where` 语义成立。

---

### 4.3 Selector::matches：recipe 如何判定元素命中

#### 4.3.1 概念说明

步骤 2 遍历 recipes 时，判断「这条 recipe 适不适用当前元素」靠的是 [`Selector`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs#L76-L104)——也就是 `show` 规则冒号左边那个「标的」。

Typst 的 selector 远不止「按元素类型匹配」一种，它是一个枚举：

- `Elem(Element, Option<dict>)`：按元素类型匹配，可选地附带字段字典（这就是 `heading.where(level: 1)`、`figure.where(kind: table)` 的底层表示）。
- `Label(Label)`：按标签匹配，如 `show <my-tag>: ...`。
- `Location(Location)`：按唯一位置匹配。
- `Regex(Regex)`：正则匹配文本——注意它在 `Selector::matches` 里**直接返回 false**（正则走的是另一套 TEXTUAL 分组机制，见 u2-l10，不在本讲的 `verdict` 路径里）。
- `Can`/`Or`/`And`/`Before`/`After`/`Within`：能力匹配与组合/位置选择器。

在 `verdict` 里真正起作用的主要是 `Elem`、`Label`、`Or`、`And` 这几种——它们决定了「这条用户 show 规则要不要套到当前元素上」。

#### 4.3.2 核心流程

`verdict` 步骤 2 里对每条 recipe 的命中判定是这样的：

```text
if !recipe.selector().is_some_and(|sel| sel.matches(elem, Some(styles))) {
    continue;  // 不匹配就跳过这条 recipe
}
```

即：recipe 没有 selector（`show: rest => ...`，已被提前消费，不会出现在这里）或 selector 不匹配，就 `continue`。注意 `matches` 的第二个参数传了 `Some(styles)`——因为 `Elem` 带字典时，需要用样式链来解析字段值（某些字段是「样式字段」，要从 chain 上读）。

`Selector::matches` 内部对 `Elem` 变体的判定逻辑可概括为：

\[
\text{matches} = (\text{target.elem} == \text{element}) \;\land\; \bigwedge_{(id,val) \in \text{dict}} \big(\text{target.get}(id, styles) == val\big)
\]

也就是说：**元素类型必须相同**，且**字典里列出的每个字段，元素的对应字段值都得相等**。字典里没列的字段不参与比较。

#### 4.3.3 源码精读

[crates/typst-realize/src/lib.rs:L469-L476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L469-L476) —— `verdict` 调用 `matches` 的地方。`recipe.selector()` 返回 `Option<&Selector>`（见 [styles.rs:L473-L476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L473-L476)），用 `is_some_and` 把「无 selector」和「selector 不匹配」两种情况都归并成「跳过」。

[crates/typst-library/src/foundations/selector.rs:L132-L155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs#L132-L155) —— `Selector::matches` 的全部分支。重点看 `Elem` 分支：先比 `target.elem() == *element`（类型相等），再 `dict.iter().flat_map(|d| d.iter()).all(...)` 逐字段比对。`Or`/`And` 是递归地复用 `matches`，非常直白。

注意底部那行：

```rust
Self::Regex(_) | Self::Before { .. } | Self::After { .. } | Self::Within { .. } => false,
```

这四种 selector 在 `matches` 里一律返回 `false`。`Regex` 不在此处处理（它的舞台是 u2-l10 的 TEXTUAL 分组），`Before`/`After`/`Within` 这类「需要全局内省」的选择器更不可能在单元素的 `verdict` 里判定，所以也都返回 false。

> 把 4.2 和 4.3 串起来：正因为 `figure.where(kind: table)` 是 `Elem` 变体、要读 `kind` 字段做比对，而 `kind` 是合成字段，`verdict` 才必须先预合成把 `kind` 补出来——这就是「预合成」与「Selector::matches」两个看似独立的模块，在 `figure.where` 这个用例上必须相互配合的原因。

#### 4.3.4 代码实践

**实践目标**：观察不同写法的 show 规则，会落到 `Selector` 的哪个变体上，进而影响 `matches` 的判定路径。

**操作步骤**：

1. 在 `Selector::matches` 的 `match self {` 之后、各分支命中处加日志，例如在 `Self::Elem(..) =>` 分支体内最前面加：
   ```rust
   eprintln!("[match] Elem {} dict={} -> {}",
       element.name(), dict.is_some(), /* 计算结果 */);
   ```
   （为简洁，可在 `matches` 返回前统一打印一次 `std::mem::discriminant(self)` 或各分支的标签。）
2. 分别用下面三段文档编译，观察命中的变体：
   ```typst
   #show heading: it => it      // Selector::Elem(heading, None)
   #show heading.where(level: 1): it => it   // Selector::Elem(heading, Some{level:1})
   #show <tag>: it => it        // Selector::Label
   = 标题 <tag>
   ```

**需要观察的现象**：

- 第一条规则 → `Elem` 变体、字典为 `None`，对所有 heading 匹配。
- 第二条规则 → `Elem` 变体、字典含 `level:1`，只对一级标题匹配。
- 第三条规则 → `Label` 变体，只对带 `<tag>` 的元素匹配。

**预期结果**：`verdict` 对「标题」元素会依次遇到这三条 recipe，按 `styles.recipes()` 的内→外顺序，命中第一条未受 guard 的 transformational 规则即停。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`show figure.where(kind: table)` 与 `show figure: it` 的 selector 有何区别？在 `Selector::matches` 里分别走哪段逻辑？

> **参考答案**：前者是 `Selector::Elem(figure, Some([(kind, table)]))`——类型相等**且** `kind` 字段等于 `table` 才匹配；后者是 `Selector::Elem(figure, None)`——只要类型是 figure 就匹配，不关心字段。两者都走 `matches` 的 `Elem` 分支，区别仅在 `dict` 是否参与逐字段比对。

**练习 2**：为什么 `Selector::Regex` 在 `matches` 里返回 false？正则 show 规则到底在哪里生效？

> **参考答案**：因为正则 show 规则要跨**连续多个文本元素**匹配，无法在「单个元素」粒度上判定，所以不在 `verdict`/`visit_show_rules` 这条路径里处理。它由 TEXTUAL 分组先把连续文本元素收集到一起，再在 `find_regex_match_in_str` / `visit_regex_match` 中统一做正则匹配与替换（详见 u2-l10）。

---

### 4.4 RecipeIndex / is_guarded：防重入与「从链顶编号」

#### 4.4.1 概念说明

替换型 show 规则有一个天然风险：**它的输出可能再次匹配同一条规则**。比如 `show heading: it => heading(level: 1)[重印] it.body`——这条规则的输出里又含一个 heading，若不加防护，`verdict` 会再次命中它，再次替换，无限套娃。

`verdict` 的步骤 2.d 就是为这道风险设的关卡。它借助两个东西：

1. **[`RecipeIndex`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L526-L528)**：一个 `pub struct RecipeIndex(pub usize)`，给样式链里的每条 recipe 编一个号。编号方式很特别——**从链顶（最外层）开始数**，越内层的 recipe 编号越大。源码注释解释了原因：「链可能向底部增长」，所以从顶部编号更稳定。
2. **[`is_guarded`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L147-L150) / [`guarded`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L152-L156)**：每个 `Content` 内部有个 `lifecycle` 位集（`SmallBitSet`）。`guarded(idx)` 把 `idx.0` 这个 bit 置 1，`is_guarded(idx)` 查这个 bit 是否为 1。

在 u2-l2 里，`visit_show_rules` 应用一条用户规则时是这样的：

```rust
ShowStep::Recipe(recipe, guard) => {
    ...
    recipe.apply(s.engine, context.track(), output.into_owned().guarded(guard))
    //                                              ^^^^^^^^^^^^^^^^^^^
    //                                  把这条规则的 index 标记到输出内容上
}
```

于是规则产出的内容带着「RecipeIndex 已套用」的标记，当它再次流回 `verdict` 时，步骤 2.d 会算出**同一个 RecipeIndex**（因为样式链没变、recipe 在链中的位置没变），`is_guarded` 返回真，这条规则被跳过——自递归就此切断。

> 还有一道独立防线：`MAX_SHOW_RULE_DEPTH = 64`，在 `visit_show_rules` 里用 `engine.route.check_show_depth()` 检查（详见 u2-l2、u3-l6）。它防的是「跨规则」循环（A 规则输出触发 B 规则、B 又触发 A）。本讲的 guard 防的是「单规则自递归」。两者互补。

#### 4.4.2 核心流程

RecipeIndex 的编号计算如下（设链中共有 `depth` 条 recipe，`r` 是 `enumerate()` 给出的、从内层起算的下标，从 0 开始）：

\[
\text{RecipeIndex} = \text{depth} - r
\]

由于 `r ∈ [0, depth)`，所以 `RecipeIndex ∈ [1, depth]`——**永远不取 0**。这不是巧合：位 0 被挪用为「是否已 prepared」的标记（见 [`is_prepared`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L158-L161) / [`mark_prepared`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L163-L166)，它们读写的是位 0）。也就是说，`lifecycle` 这一个位集同时承载了两类生命周期信息：

| bit 位 | 含义 | 由谁置位 |
| --- | --- | --- |
| 0 | 元素是否已 `prepare` | `prepare` 末尾的 `mark_prepared` |
| ≥1 | 某个 `RecipeIndex` 的 show 规则是否已套用 | `visit_show_rules` 应用规则时的 `.guarded(guard)` |

`verdict` 步骤 2 的完整防重入逻辑：

```text
let index = RecipeIndex(depth - r);   // 计算这条 recipe 的「从链顶编号」
if elem.is_guarded(index) {            // 元素身上是否已标记「此规则套过」
    continue;                          // 是 → 跳过，防自递归
}
step = Some(ShowStep::Recipe(recipe, index));  // 否 → 采用，并把 index 带回去做标记
```

#### 4.4.3 源码精读

[crates/typst-realize/src/lib.rs:L490-L496](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L490-L496) —— `verdict` 计算 `index` 并查 `is_guarded` 的地方。`depth` 由上一行的 [`LazyCell`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L463-L467) 懒计算（`styles.recipes().count()`），只有在真有 recipe 匹配时才算，省一次全链计数。

[crates/typst-library/src/foundations/content/mod.rs:L147-L166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L147-L166) —— 四个辅助方法。注意 `is_prepared` 查的是 `lifecycle.contains(0)`、`mark_prepared` 插的是 `lifecycle.insert(0)`；而 `is_guarded(index)` 查 `lifecycle.contains(index.0)`、`guarded(index)` 插 `lifecycle.insert(index.0)`。**同一个位集、同一套 API**，靠「0 vs 非 0」区分两类语义。

[crates/typst-library/src/foundations/styles.rs:L526-L528](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L526-L528) —— `RecipeIndex` 就是一个透明的 `pub struct RecipeIndex(pub usize)`，本身不含逻辑，只是把「编号」这个整数包成一个类型，避免和别的 usize 混用。

> 小结这套机制的精妙：它**不需要**记住「套用过哪些规则」的一个集合，而是利用「规则在链中的位置」+「元素自带的位集」两点，把防重入信息**贴在内容上随其流动**。一条规则的输出走到哪儿，它的 guard 就带到哪儿，下一次 `verdict` 自然能识别。

#### 4.4.4 代码实践

**实践目标**：构造一个「输出里又含同类元素」的 show 规则，观察 guard 如何在第二轮 `verdict` 里阻止同一规则再次命中。

**操作步骤**：

1. 在 `verdict` 的 `if elem.is_guarded(index) {` 分支（L492 附近）加日志：
   ```rust
   if elem.is_guarded(index) {
       eprintln!("[guard] SKIP recipe index={} on elem={:?} (guarded)", index.0, elem.func().name());
       continue;
   }
   ```
2. 在 `step = Some(ShowStep::Recipe(recipe, index));`（L496）之后加：
   ```rust
   eprintln!("[guard] APPLY recipe index={} on elem={:?}", index.0, elem.func().name());
   ```
3. 用一个「自引用」show 规则编译：
   ```typst
   #show heading: it => {
     [前缀]
     heading(level: it.level)[复刻 #it.body]
   }
   = 标题
   ```

**需要观察的现象**：

- 第一轮：原 `= 标题` 命中规则，打印 `APPLY index=...`；规则产出的 `heading` 被标记上该 index。
- 第二轮：产出内容里的 `heading` 再次进入 `verdict`，**同一条规则**算出同一个 index，`is_guarded` 返回真，打印 `SKIP ... (guarded)`。

**预期结果**：自递归被 guard 切断，规则只生效一次；不会再无限套用。若把 `guarded(guard)` 那行临时改成不标记，则会看到 `APPLY` 反复打印，直至撞上 `MAX_SHOW_RULE_DEPTH = 64` 报错。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`RecipeIndex` 为什么要「从链顶编号」而不是「从链底（内层）编号」？

> **参考答案**：因为样式链「可能向底部（内层）增长」——比如在内层又叠了一层样式。如果从内层编号，那么内层每多一条 recipe，外层 recipe 的编号都会跟着变，guard 标记就失效了。从外层（顶部）编号则保证：一条 recipe 的 index 由它在「外层不动的那部分」中的位置决定，不受内层增长影响，guard 标记始终能对上同一条规则。

**练习 2**：位 0 为什么能安全地复用为「prepared 标记」，而不与 RecipeIndex 冲突？

> **参考答案**：因为 `RecipeIndex = depth - r`，而 `r ∈ [0, depth)`，所以 `RecipeIndex ∈ [1, depth]`，永远不可能为 0。于是位 0 永远不会被任何 RecipeIndex 占用，可以放心留给 `prepared` 标记。这是「编号从 1 起」与「位 0 另有他用」之间一个刻意的小约定。

**练习 3**：guard 能防止「规则 A 输出触发规则 B、规则 B 又触发规则 A」这种跨规则循环吗？为什么？

> **参考答案**：不能。guard 只阻止「同一条规则（同一 RecipeIndex）」对同一内容二次套用；跨规则循环里，A 与 B 是不同的 RecipeIndex，guard 互不干涉。这种循环由另一道防线 `MAX_SHOW_RULE_DEPTH = 64`（`engine.route.check_show_depth`）来兜底，详见 u2-l2 与 u3-l6。

---

## 5. 综合实践

把本讲的四个模块串成一条完整的「预审」追踪链。请完成下面的综合任务：

**任务**：用一个文档同时触发「预合成 + selector 字段匹配 + guard 防重入」，绘制 `verdict` 对其中关键元素的完整判决轨迹。

**文档**：

```typst
// 一条「按合成字段筛选」的 show 规则
#show figure.where(kind: table): it => [表] + it

// 一条「输出里又含 figure」的自引用规则（用来观察 guard）
#show figure: it => {
  [包装]
  figure(it.body)
}

#figure(table(columns: 1, [数据]))
```

**要求**：

1. 在 `verdict` 的以下位置加日志（参考各小节的实践步骤）：预合成前后 `kind` 字段、`Selector::matches` 的命中变体、`is_guarded` 的 SKIP/APPLY、最终返回 `None` 还是 `Some`。
2. 跑一次编译，按时间顺序记录日志，整理成一张「轨迹表」，列：`步骤 | 判定结果 | 关键字段/index`。
3. 用一段话解释：为什么第一条 `figure.where(kind: table)` 能命中——预合成在其中起了什么作用？为什么自引用的第二条规则没有造成无限递归——guard 在哪一步切断了它？
4. 进阶思考：如果交换两条 show 规则的书写顺序（内层/外层互换），`verdict` 的判决会怎样变化？这与 `styles.recipes()` 的「内→外」遍历顺序有什么关系？

**预期**：你能用本讲学到的「预合成补字段 → selector 字段比对 → guard 贴标防递归 → 内置规则兜底 → None 放行」这一整条逻辑，自洽地解释日志里的每一次判决。具体日志**待本地验证**。

## 6. 本讲小结

- `verdict` 是 show 规则流水线的「预审」环节：读元素、审 recipes、查内置规则，产出一份 `Verdict { prepared, map, step }` 判决书；当「无可做」时返回 `None`，让 `visit_show_rules` 放弃认领。
- **预合成**是 `verdict` 最无奈也最关键的一步：先 `clone` 元素、在克隆体上跑一次 `synthesize`，才能让 `figure.where(kind: table)` 这类按合成字段筛选的 selector 在「正式合成之前」就能命中。
- `Selector::matches` 负责 recipe 的命中判定：`Elem` 变体按「类型 + 字段字典」比对，`Label` 按标签比对，`Regex`/`Before`/`After`/`Within` 在此一律返回 false（它们走别的路径）。
- **show-set 规则**（`Transformation::Style`）被收进 `map`，**不**产出 `ShowStep`；只有 transformational 规则（替换内容）才记成 `ShowStep::Recipe`，且「内→外」遍历只取第一条。
- **RecipeIndex + is_guarded** 构成单规则防重入：编号「从链顶起、取值 ≥1」，位 0 留给 `prepared` 标记；规则产出内容时贴上 index，下一轮 `verdict` 据此跳过同一条规则，切断自递归。
- 内置 show 规则在「没有用户规则命中」时兜底，且按 `(元素, target)` 在 `NativeRuleMap` 里查找；用户规则始终优先于内置规则。

## 7. 下一步学习建议

- **下一讲 u2-l4 `prepare`** 将拆开 `Verdict.prepared` 这个布尔背后的真正动作：分配 location、应用内置 `ShowSet`、正式 `synthesize`、`materialize` 样式字段、生成 start/end tag、`mark_prepared` 保证幂等。本讲说的「预合成的正式版本就在 prepare 里」会在那里得到完整印证。
- 想看「判决执行端」的全貌，可回看 u2-l2 `visit_show_rules`——本讲的 `verdict` 正是它的第一步。
- 对正则 show 规则（`Selector::Regex`）如何跨文本元素匹配感兴趣，可预先翻到 u2-l10，了解 TEXTUAL 分组为何不经过 `verdict` 这条路径。
- 对「跨规则循环」的第二道防线 `MAX_SHOW_RULE_DEPTH` 与分组深度上限感兴趣，可提前阅读 u3-l6。
