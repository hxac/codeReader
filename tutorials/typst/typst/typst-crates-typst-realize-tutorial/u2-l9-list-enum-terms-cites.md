# 列表/枚举/术语与引用分组

## 1. 本讲目标

本讲聚焦 typst-realize 里 priority 同为 2 的「列表家族」与「引用」分组规则。学完后你应当能够：

- 说清 `list_like_grouping::<T>()` 这个 `const fn` 泛型如何用**同一份代码**生成 `LIST` / `ENUM` / `TERMS` 三条几乎相同的 `GroupingRule`，以及 `ListLike` / `ListItemLike` 这两个 trait 在其中扮演的角色。
- 读懂 `finish_list_like` 的两件核心工作：用「组内是否出现 `ParbreakElem`」判定 **tight（紧凑）**；用 **trunk / suffix** 把各 item 的公共样式提到列表外、把局部差异折进每个 item 的 body。
- 说清 `CITES` 规则如何把连续的 `CiteElem` 收集成一个 `CiteGroup`，以及 `finish_cites` 为何比 `finish_list_like` 简单得多。

本讲是 u2-l7（分组生命周期）与 u2-l8（段落分组 / `repack`）的自然延续：u2-l7 给出了分组启动→收尾的通用骨架，u2-l8 展示了 `repack` 这套「trunk 提到外、suffix 包回去」的打法，本讲则把同一套思路应用到列表与引用上——你会发现 `finish_list_like` 其实是 `repack` 的「逐项独立」简化版。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些在前几讲建立的概念（本讲直接复用，不再重复定义）：

- **realization（具现化）**：把任意 content 树递归套用样式与 show 规则，规整成扁平的、全部由后端已知元素组成的 pair 清单。入口 `realize()` 输出 `Vec<Pair<'a>>`，其中 `Pair<'a> = (&'a Content, StyleChain<'a>)`。（见 u1-l1、u1-l2）
- **`GroupingRule` 静态说明书**：含 `priority`、`tags`、`effect`、`interrupt`、`finish` 五个字段；`effect(content)` 返回 `GroupingEffect::{Trigger, Inner, Neutral, Interrupt}` 描述元素与本分组的关系。（见 u2-l6）
- **分组栈与优先级嵌套**：`State.groupings` 是容量为 `MAX_GROUP_NESTING = 3` 的栈；**新规则 priority 须严格高于栈顶才能嵌套其内**，六条规则的 priority 只有 `{1,2,3}` 三档，列表与引用都是 2。（见 u2-l1、u2-l7）
- **分组生命周期**：`visit_grouping_rules` 负责「嵌套判定 → 并入判定 → 收尾判定 → 启动新分组」；收尾时 `finish_grouping` 先做尾部裁剪与 tag 处理，再调用 `(rule.finish)(Grouped { s, start })`，最后由 `finish_*` 产出新元素并 `visit` 回喂。（见 u2-l7）
- **trunk / suffix 样式拆分**：`repack`（见 u2-l8）把所有 pair 样式链的**最长公共前缀**称为 **trunk**，提到元素外面；每个 pair 上**多出来**的局部样式称为 **suffix**，重新包成 `StyledElem` 留在内部。本讲的 `finish_list_like` 复用同一思想。
- **`Grouped` 视图**：`get()` 取 `&sink[start..]`、`end()` 把 sink 截断到 `start` 并交还 `State`，供 `finish_*` 产出新元素后继续 `visit`。（见 u2-l7）

一个直觉铺垫：**为什么列表能复用 `repack` 的思路却又更简单？** 在段落里，相邻同样式的文本会被 `group_by_key` 合并、再用 `Content::sequence(...).styled_with_map(suffix)` 整段打包；而列表的每个 item 天然是一个独立子元素，**无需合并相邻项**，只要逐个 item 把它自己的局部 suffix 折进 body 即可。所以 `finish_list_like` 不需要 `group_by_key`，只需对每个 item 调一次 `T::Item::styled`。

## 3. 本讲源码地图

本讲主要涉及一个文件，外加两处 typst-library 的支撑定义：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | `LIST`/`ENUM`/`TERMS`/`CITES` 四条规则定义、泛型生成器 `list_like_grouping`、`finish_list_like` 与 `finish_cites`，以及它们参与的规则表 |
| `crates/typst-library/src/model/list.rs` | `ListLike` / `ListItemLike` 两个 trait 及其对 `ListElem` / `ListItem` 的实现，还有 `tight` 字段的语义说明 |
| `crates/typst-library/src/foundations/styles.rs` | `StyleChain::trunk` / `suffix` / `links`，是 `finish_list_like` 样式拆分的底层支撑 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**list_like_grouping 与 LIST/ENUM/TERMS**、**finish_list_like**、**CITES 与 finish_cites**。

### 4.1 用泛型统一三种列表：list_like_grouping

#### 4.1.1 概念说明

Typst 有三种「列表」元素：无序列表 `ListElem`（`-` 语法）、有序列表 `EnumElem`（`+` / `1.` 语法）、术语列表 `TermsElem`（`/ Term: Desc` 语法）。从**分组**的角度看，它们的行为几乎一模一样：

- 都是块级容器，由一连串「项（item）」组成；
- 都把相邻的同类型 item 收拢成一个列表元素；
- 收尾时都要决定列表是紧凑（tight）还是宽松，并打包出最终元素。

唯一不同的是「项的类型」和「最终容器的类型」。为此 typst-realize 用一个 `const fn` 泛型 `list_like_grouping::<T>()`（`T: ListLike`）把这份共同逻辑写一遍，再实例化三次得到三条规则：

[crates/typst-realize/src/lib.rs:L1093-L1100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1093-L1100) —— `LIST`/`ENUM`/`TERMS` 三条规则就是 `list_like_grouping::<ListElem>()` / `::<EnumElem>()` / `::<TermsElem>()` 三次实例化。

`T` 必须实现 `ListLike` trait，它把「项类型」与「构造方法」抽象出来：

[crates/typst-library/src/model/list.rs:L224-L231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L224-L231) —— `ListLike`：关联类型 `type Item: ListItemLike`（项的类型）与方法 `fn create(children, tight) -> Self`（由子项和紧凑度构造容器）。

项类型则实现 `ListItemLike`，提供「把局部样式折进 body」的能力：

[crates/typst-library/src/model/list.rs:L233-L237](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L233-L237) —— `ListItemLike`：`fn styled(item, styles) -> Packed<Self>`，把样式作用到项上（对 `ListItem` 而言是折进 `body`）。

有了这两个 trait，`list_like_grouping` 就能在「不知道具体是哪种列表」的情况下，用 `T::Item::ELEM` 判定触发、用 `finish_list_like::<T>` 收尾、用 `T::create` 构造——这正是泛型复用的价值。

#### 4.1.2 核心流程

`list_like_grouping` 生成的规则骨架如下，关键是 `effect` 闭包对元素角色的判定：

[crates/typst-realize/src/lib.rs:L1102-L1120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1102-L1120) —— `list_like_grouping`：`priority: 2`、`tags: false`、`finish: finish_list_like::<T>`，`effect` 按 `T::Item` 判定。

`effect` 的判定可以用一张表概括（注意 `ParbreakElem` 的特殊待遇）：

| 元素 | `effect` | 含义 |
| --- | --- | --- |
| `T::Item`（`ListItem` / `EnumItem` / `TermItem`） | `Trigger` | 列表项，**触发**并栖居本分组 |
| `SpaceElem` / `ParbreakElem` | `Inner` | 只能在分组**内部**出现，不触发也不打断 |
| 其它一切 | `Interrupt` | 非列表内容，**打断**当前列表 |

此外两个静态字段：

- `priority: 2`：与 `CITES` 同级，高于 `PAR`（1）、低于 `TEXTUAL`（3）。因此列表可以嵌套在段落之内（一个段落里可以有一段文字 + 一个列表 + 又一段文字）。
- `tags: false`：列表**不自管标签**。这意味着 `finish_grouping` 不会替它做 before/within/after 的 tag 边界扩张，而是把分组范围内的 `TagElem` **剥离**出来、收尾后再单独 `visit`（详见 4.1.3）。
- `interrupt: |elem| elem == T::ELEM || elem == AlignElem::ELEM`：遇到**同类型列表**（防嵌套同款）或 `set align(...)` 样式时收尾当前列表。

> **关键设计**：为什么 `ParbreakElem` 是 `Inner` 而不是 `Interrupt`？因为用户用「项之间空一行」来表达**宽松列表**。这个空行在 content 流里就是一个 `ParbreakElem`。如果它是 `Interrupt`，列表遇到空行就会立即收尾，于是宽松列表会被拆成好几个单元素列表。让它保持 `Inner`，整个宽松列表才能留在同一个分组里，收尾时 `finish_list_like` 再凭「组内有没有 `ParbreakElem`」把 `tight` 置为 `false`（见 4.2）。这是理解列表分组最重要的一点。

#### 4.1.3 源码精读

**第一，三条规则出现在哪些规则表里。** 列表与引用不只服务于文档/片段具现化，在段落和数学具现化里同样存在：

[crates/typst-realize/src/lib.rs:L1008-L1015](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1008-L1015) —— `FLOW_RULES`（Document/Fragment）含全部六条；`PAR_RULES`（段落内部）与 `MATH_RULES`（数学内部）都不含 `&PAR`，但都含 `&LIST/&ENUM/&TERMS`（及 `&CITES`）。

也就是说，列表分组在 `Document`、`Fragment`、`Par`、`Math` 四种 kind 下都会启用——这也是为什么数学环境里也能写列表。

**第二，`tags: false` 在收尾时到底发生了什么。** `finish_grouping` 在调用 `finish_*` 之前，对 `!rule.tags` 的规则会专门做一次「剥离 + 压实」：

[crates/typst-realize/src/lib.rs:L954-L970](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L954-L970) —— 当 `!rule.tags` 时，把分组范围内的 `TagElem` 收集到 `tags`、其余 pair 用 `copy_within` 风格的原地左移压实，再 `truncate`。

收尾（`finish_list_like` 产出了 `ListElem` 并 `visit`）之后，被剥离的 `tags` 会和分组尾部的 `tail` 一起重新喂回流水线：

[crates/typst-realize/src/lib.rs:L975-L978](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L975-L978) —— 对 `tags` 与 `tail` 里的每个 pair 调 `visit`，让它们各自落到 sink。

效果是：列表项内部如果有带 `label` 的可定位元素（会生成 `Tag::Start`/`Tag::End`），这些 tag **不会**被吞进 `ListElem.body`，而是被提出来、与列表元素并列地留在 sink 里，保证内省（query/ref）能正确跨列表边界工作。这与 `PAR`/`TEXTUAL`（`tags: true`）自管标签、把 tag 纳入 body 的策略正好相反。

#### 4.1.4 代码实践

**实践目标**：验证「`ParbreakElem` 作为 `Inner` 让宽松列表留在同一分组」这一设计。

**操作步骤**（源码阅读型）：

1. 打开 `crates/typst-realize/src/lib.rs`，定位 `list_like_grouping`（L1102-L1120）与 `finish_list_like`（L1219-L1242）。
2. 在 `effect` 闭包的 `ParbreakElem` 分支上做心智推演：一个宽松列表 `- A` / 空行 / `- B` 的 content 流大致是 `[ListItem(A), SpaceElem?, ParbreakElem, ListItem(B)]`，逐个套 `effect`：
   - `ListItem(A)` → `Trigger`，启动 LIST 分组并入栈；
   - `ParbreakElem` → `Inner`，**并入**分组（不收尾）；
   - `ListItem(B)` → `Trigger`，`matching` 是 LIST，priority 与栈顶相等（不严格更高），不嵌套；对栈顶 `effect(ListItem) == Trigger ≠ Interrupt`，于是**并入**分组。
3. 跟到 `finish_list_like`：`tight = !elems.iter().any(|(c, _)| c.is::<ParbreakElem>())`，因组内确有 `ParbreakElem`，`tight` 应为 `false`。

**需要观察的现象**：宽松列表不会被拆成两个单元素列表，而是作为一个 `tight=false` 的整体被收尾。

**预期结果**：`finish_list_like` 收到的 `elems` 里含有 `ParbreakElem`，最终 `T::create(children, tight=false)`。具体日志输出待本地验证（见 4.2.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接写三条各自独立的 `LIST`/`ENUM`/`TERMS` 规则，而要用 `list_like_grouping::<T>()` 泛型？
> **答案**：因为三条规则的 `priority`、`tags`、`effect` 判定逻辑、`interrupt` 条件完全一致，差别只在「项类型」与「构造方法」。用泛型 + `ListLike` trait 把差异参数化，可以避免三份几乎相同的代码复制粘贴，未来要改分组行为只需改一处。`const fn` 还保证这三条规则在编译期就生成好、成为真正的 `static`。

**练习 2**：一个 `ListItem` 流经 `visit_grouping_rules`，此时栈顶是 `PAR` 分组（priority 1）。会发生什么？
> **答案**：`matching` = LIST（priority 2）。循环里判定 `matching.priority (2) > active.priority (1)` 成立 → `break`，跳出继续循环，转去「启动新分组」：`push` 一个 LIST 分组嵌套在 PAR 之内，并把该 `ListItem` 入栈。所以列表会作为一个更内层的分组嵌套在段落里。

**练习 3**：`list_like_grouping` 的 `interrupt` 写的是 `elem == T::ELEM`，为什么？
> **答案**：`T::ELEM` 是容器自身的元素 id（如 `ListElem::ELEM`）。遇到一个**已经成型**的同类型列表元素就收尾当前分组，避免把两个独立列表错误地粘成一个。注意这里的嵌套子列表（还是一个 `ListItem` 的 body）不会走到这一步——子列表是 `ListElem`，它对当前 LIST 规则的 `effect` 是 `Interrupt`（非 item、非 space/parbreak），同样会打断当前列表，从而保证嵌套列表各自独立。

---

### 4.2 finish_list_like：tight 判定与 trunk/suffix 样式拆分

#### 4.2.1 概念说明

当 LIST/ENUM/TERMS 分组收尾时，`finish_grouping` 调用 `(rule.finish)(Grouped { s, start })`，对这三条规则来说就是 `finish_list_like::<T>`。它要做两件核心工作：

1. **判定 tight（紧凑度）**：扫描组内元素，若**没有**任何 `ParbreakElem`，则 `tight = true`（紧凑，项之间无空行）；否则 `tight = false`（宽松）。这个布尔值最终传给 `T::create(children, tight)`，对 `ListElem` 而言就是 `ListElem::new(children).with_tight(tight)`。
2. **拆分样式（trunk / suffix）**：和 `repack`（见 u2-l8）同源的思想——把所有 item 共享的**公共样式前缀** trunk 提到列表外面（交给 `visit(ListElem, trunk)`），每个 item 上**多出来**的局部样式 suffix 则折进该 item 的 body。

`tight` 的用户可见语义在 `ListElem` 的字段文档里说得很清楚：

[crates/typst-library/src/model/list.rs:L44-L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L44-L66) —— `tight` 字段：markup 模式下，项之间无空行则 `true`、有空行则 `false`，且「markup 定义的紧凑度无法被 set 规则覆盖」。

#### 4.2.2 核心流程

`finish_list_like` 分四步：

1. **取元素与 span**：`elems = grouped.get()` 取 `&sink[start..]`；`span = select_span(elems)` 取第一个非 detached 的 span 作为新元素的来源定位。
2. **判 tight**：`tight = !elems.iter().any(|(c, _)| c.is::<ParbreakElem>())`。
3. **拆样式**：只取**item**的样式链算 trunk；`trunk_depth = trunk.links().count()`；对每个 item 取 `local = s.suffix(trunk_depth)` 并用 `T::Item::styled(item, local)` 折进 body。
4. **建元素并回喂**：`grouped.end()` 截断 sink；`T::create(children, tight).pack().spanned(span)`；`s.store(...)` 延长生命周期后 `visit(s, elem, trunk)`。

用伪代码概括：

```
elems = sink[start..]                      # 含 item + 残留的 Space/Parbreak
tight = (elems 里没有任何 ParbreakElem)
styles = [ pair.styles  for pair in elems if pair 是 T::Item ]   # 只看 item
trunk = 公共前缀(styles)                   # 所有 item 共享的样式层
depth = trunk 的层数
children = []
for (c, s) in elems:
    if c 是 T::Item:
        local = s 去掉前 depth 层剩下的部分  # 该 item 独有的差异样式
        children.append( T::Item::styled(c, local) )
visit( T::create(children, tight), trunk )   # trunk 作用于整个列表
```

注意与 `repack` 的区别：这里**不**用 `group_by_key` 合并相邻同样式 item，因为每个 item 都是独立子元素；也**不**把 suffix 包成 `Content::sequence(...).styled_with_map(...)`，而是直接折进每个 item 的 body（`ListItem::styled` → `item.body.style_in_place`，见 [list.rs:L247-L252](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L247-L252)）。

#### 4.2.3 源码精读

`finish_list_like` 全貌：

[crates/typst-realize/src/lib.rs:L1219-L1242](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1219-L1242) —— `finish_list_like`：取元素 → 判 tight → 算 trunk → 逐项折 suffix → `end()` 截断 → `T::create` 后 `visit`。

逐行说明：

- `let elems = grouped.get();`：[lib.rs:L223-L226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L223-L226)，取本分组这段 pair 切片（已由 `finish_grouping` 剥离过 tag、压实过）。`elems` 里既有 item，也可能有作为 `Inner` 混入的 `SpaceElem`/`ParbreakElem`。
- `let span = select_span(elems);`：[lib.rs:L1506-L1509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1506-L1509)，`Span::find` 取第一个非 detached 的 span。
- `let tight = !elems.iter().any(|(c, _)| c.is::<ParbreakElem>());`：**只要组内出现过任何一个 `ParbreakElem`，`tight` 即为 `false`**。这是「宽松列表」的唯一判定依据。
- `let styles = elems.iter().filter(|(c, _)| c.is::<T::Item>()).map(|&(_, s)| s);`：**只收集 item 的样式链**，刻意忽略 `SpaceElem`/`ParbreakElem`（它们不参与公共样式计算）。
- `let trunk = StyleChain::trunk(styles).unwrap();`：trunk 是这些 item 样式链的最长公共前缀。`unwrap` 安全，因为分组至少由一个 `Trigger` 的 item 启动，`styles` 非空。`trunk` 的算法见 [styles.rs:L731-L758](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L731-L758)：以第一条链为基准，遇到更短的链就不断 `pop` 基准尾部，直到所有链在最浅深度处一致。
- `let trunk_depth = trunk.links().count();`：trunk 由多少层「链接」组成。`links()` 见 [styles.rs:L702-L704](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L702-L704)。
- 构建 `children` 的 `filter_map`：
  - `c.to_packed::<T::Item>()?`：只保留 item（`?` 让非 item 的 space/parbreak 直接被跳过）；`.clone()` 得到拥有的 `Packed<T::Item>`。
  - `let local = s.suffix(trunk_depth);`：取该 item 样式链**在 trunk 之后**的局部差异样式，见 [styles.rs:L715-L723](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L715-L723)。
  - `T::Item::styled(item, local)`：把局部样式折进 item；对 `ListItem` 即 `item.body.style_in_place(local)`（[list.rs:L247-L252](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L247-L252)）。
- `let s = grouped.end();`：[lib.rs:L233-L238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L233-L238)，`truncate(start)` 把这段从 sink 删除并交还 `State`。
- `let elem = T::create(children, tight).pack().spanned(span);`：`T::create` 对 `ListElem` 是 `Self::new(children).with_tight(tight)`（[list.rs:L239-L245](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/list.rs#L239-L245)）。`s.store(elem)` 放进 arena 延长生命周期，再 `visit(s, elem, trunk)` 重新进入流水线——trunk 作为「列表级公共样式」作用于整个 `ListElem`，新元素最终走兜底 `push` 落进 sink。

> 小结这一步的样式去向：**公共样式 trunk → 作用于整个 `ListElem`**；**每项的差异 suffix → 折进各自 item 的 body**。这与 `repack`「trunk 到外、suffix 包回 body 内」是同一个模型，只是列表无需合并相邻项。

#### 4.2.4 代码实践

**实践目标**：亲手对比「紧凑列表」与「宽松列表」在 `finish_list_like` 里的 `tight` 取值，并观察 trunk 提取。

**操作步骤**（需本地编译 typst；以下源码改动请在你自己的本地 clone 上进行，仅为观察用途）：

1. 在 `crates/typst-realize/src/lib.rs` 的 `finish_list_like` 里，算出 `tight` 与 `trunk` 之后插入日志：

   ```rust
   let tight = !elems.iter().any(|(c, _)| c.is::<ParbreakElem>());
   let styles = elems.iter().filter(|(c, _)| c.is::<T::Item>()).map(|&(_, s)| s);
   let trunk = StyleChain::trunk(styles).unwrap();
   eprintln!("[finish_list_like] T={}, elems={}, items={}, tight={}, trunk_links={}",
       std::any::type_name::<T>(), elems.len(),
       elems.iter().filter(|(c, _)| c.is::<T::Item>()).count(),
       tight, trunk.links().count());
   ```

2. 准备两个 Typst 文档（示例代码）。紧凑列表：

   ```typst
   % tight.typ —— 项之间无空行
   #set page(width: 240pt, height: auto)
   - Apple
   - Banana
   - Cherry
   ```

   宽松列表：

   ```typst
   % loose.typ —— 项之间有空行
   #set page(width: 240pt, height: auto)
   - Apple

   - Banana

   - Cherry
   ```

3. 分别编译并捕获 stderr：

   ```bash
   cargo build --release -p typst
   ./target/release/typst compile tight.typ tight.pdf 2> realize.log
   ./target/release/typst compile loose.typ loose.pdf 2>> realize.log
   ```

**需要观察的现象**：在 `realize.log` 里筛选出 `T=...ListItem` 的记录（内省会多次重跑 realize，故日志会重复）：
- `tight.typ`：对应记录 `tight=true`，`elems` 与 `items` 相近（组内无 `ParbreakElem`）。
- `loose.typ`：对应记录 `tight=false`，`elems` 比 `items` 多（多出来的正是作为 `Inner` 混入的 `SpaceElem`/`ParbreakElem`）。

**预期结果**（待本地验证）：两份文档的 `tight` 一真一假，正好对应 `ListElem::create(children, tight)` 传入的 `tight` 参数；`trunk_links` 在两份文档里相同（都只含 `#set page` 之类列表级公共样式，没有逐项差异时 trunk 就是全部链）。

> 说明：若不想编译，可改为纯源码阅读——对照 4.2.2 的伪代码，手工对两个文档分别跑一遍 `tight` 判定与 `filter_map` 流程，得出 `children` 与 `tight`。

#### 4.2.5 小练习与答案

**练习 1**：`finish_list_like` 计算 trunk 时为什么用 `.filter(|(c, _)| c.is::<T::Item>())` 把 space/parbreak 排除，而不是用全部 `elems`？
> **答案**：因为 `SpaceElem`/`ParbreakElem` 的样式链并不代表「列表项的样式」，把它们算进去会污染公共前缀。tight 关心的是「有没有 parbreak」（用 `.any(...)` 单独判），而样式拆分只该考虑真正成为子项的 item。

**练习 2**：`T::Item::styled(item, local)` 把局部样式折进了 item 的 body，而不是挂在 item 元素本身上。这会带来什么差别？
> **答案**：对 `ListItem`，`styled` 调 `item.body.style_in_place(local)`，把样式推进 body 这棵 content 子树里。这样局部差异样式只作用在「这一项的内容」上，而列表级属性（marker、indent、spacing 等）由 trunk 作用于整个 `ListElem` 来统一控制。两者各司其职，避免了把整段样式都重复挂到每个 item 上。

**练习 3**：为什么 `trunk = StyleChain::trunk(styles).unwrap()` 的 `unwrap` 是安全的？
> **答案**：一个 LIST/ENUM/TERMS 分组必然由某个 `Trigger` 元素启动，而 `Trigger` 的只有 `T::Item`。所以 `elems` 里至少有一个 item，`styles` 迭代器至少产出一个样式链，`trunk` 对非空输入返回 `Some`，`unwrap` 不会 panic。

---

### 4.3 CITES 规则与 finish_cites：引用分组

#### 4.3.1 概念说明

在 Typst 里，连续的文献引用（如 `@arrgh @netwok` 或 `#cite(<a>) #cite(<b>)`）会被收拢成一个 `CiteGroup`，由排版阶段统一解析格式（决定分隔符、是否合并为「a, b」等）。负责这件事的分组规则就是 `CITES`，它与列表同属 priority 2，但细节有几处关键不同。

`CiteElem` 是单条引用元素（带一个 `key: Label` 字段指向文献条目），`CiteGroup` 是它的容器。`finish_cites` 把连续的 `CiteElem` 收集成 `CiteGroup::new(children)`。

#### 4.3.2 核心流程

`CITES` 规则本体：

[crates/typst-realize/src/lib.rs:L1073-L1091](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1073-L1091) —— `CITES`：`priority: 2`、`tags: false`、`finish: finish_cites`。

`effect` 判定（注意与列表的差异）：

| 元素 | `effect` | 含义 |
| --- | --- | --- |
| `CiteElem` | `Trigger` | 单条引用，**触发**并栖居本分组 |
| `SpaceElem` | `Inner` | 引用之间的空格可在分组内部（如 `@a @b` 中间的空格） |
| 其它一切（**含 `ParbreakElem`**） | `Interrupt` | 非引用内容，**打断**当前引用组 |

与列表最大的区别：`ParbreakElem` 对 CITES 是 `Interrupt`，而不是 `Inner`。因为引用没有「紧凑/宽松」的概念——空行就应当把引用组断开。`interrupt` 字段为 `CiteGroup`/`Par`/`Align`：遇到已成型的 `CiteGroup`、段落样式或对齐样式都收尾。

`finish_cites` 的流程比 `finish_list_like` 简单得多：取元素 → 取 span → **直接以第一个元素的样式链为 trunk** → 把所有 `CiteElem` 克隆成 children → `end()` 截断 → `CiteGroup::new(children)` 后 `visit`。

#### 4.3.3 源码精读

`finish_cites` 全貌：

[crates/typst-realize/src/lib.rs:L1205-L1217](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1205-L1217) —— `finish_cites`：取元素 → 取 span → `trunk = elems[0].1` → 克隆所有 `CiteElem` → `end()` → `CiteGroup::new(children)` 后 `visit`。

逐行说明：

- `let elems = grouped.get();` 与 `let span = select_span(elems);`：与 `finish_list_like` 相同。
- `let trunk = elems[0].1;`：**没有**调用 `StyleChain::trunk` 求公共前缀，而是直接取**第一个元素**的样式链作为整组的 trunk。原因是同组内的连续 `CiteElem` 共享同一条样式链（它们在源码里通常处于同一段行内样式下），没必要再算前缀。
- `let children = elems.iter().map(|(c, _)| (**c).clone()).collect();`：把每个 `CiteElem` 原样克隆成 `Packed<CiteElem>`，**不做** per-item 的 suffix 拆分——所有引用共用 trunk 样式，差异为零。
- `let s = grouped.end();`：截断 sink。
- `let elem = CiteGroup::new(children).pack().spanned(span);`：构造引用组；`visit(s, s.store(elem), trunk)` 回喂。

把三种 `finish_*` 放在一起对比，差异一目了然：

| 维度 | `finish_par`（repack） | `finish_list_like` | `finish_cites` |
| --- | --- | --- | --- |
| tight 判定 | 无 | 有（组内有无 `ParbreakElem`） | 无 |
| trunk 计算 | `trunk_from_pairs`（过滤 tag） | `trunk`（仅 item 样式链） | 直接取 `elems[0].1` |
| suffix 处理 | `group_by_key` 分段 + `StyledElem` 包回 | 逐 item `suffix(depth)` 折进 body | 无（不拆分） |
| tags 策略 | `tags: true`（自管，纳入 body） | `tags: false`（剥离后重访） | `tags: false`（剥离后重访） |
| 产物 | `ParElem` | `ListElem`/`EnumElem`/`TermsElem` | `CiteGroup` |

一句话：**列表需要 tight + 逐项样式，引用两者都不需要**，所以 `finish_cites` 最简。

#### 4.3.4 代码实践

**实践目标**：观察连续引用如何被收成 `CiteGroup`，以及空行如何打断引用组。

**操作步骤**（需本地编译 typst；源码改动仅为观察用途）：

1. 在 `crates/typst-realize/src/lib.rs` 的 `finish_cites` 开头插入日志：

   ```rust
   fn finish_cites(grouped: Grouped) -> SourceResult<()> {
       let elems = grouped.get();
       eprintln!("[finish_cites] children={}", elems.len());
       // ...原逻辑
   ```

2. 准备一个含文献引用的文档（示例代码）：

   ```typst
   % cites.typ
   #bibliography("works.bib")
   First group: @arrgh @netwok.

   Second group after blank line:
   @arrgh @netwok.
   ```

   （需要同目录下有一份 `works.bib`，含 `arrgh`、`netwok` 两个条目；若无，可省略 bibliography，仅观察 realize 阶段的分组行为。）

3. 编译并捕获 stderr：`./target/release/typst compile cites.typ cites.pdf 2> realize.log`。

**需要观察的现象**：`realize.log` 里应出现**多条** `[finish_cites]` 记录——因为两段引用被中间的空行（`ParbreakElem`，对 CITES 是 `Interrupt`）断开，各自形成独立的 `CiteGroup`，每条记录 `children=2`。

**预期结果**（待本地验证）：两段 `@arrgh @netwok` 各自被收成一个含 2 个 `CiteElem` 的 `CiteGroup`；若把两段之间的空行删掉合并成一段，则它们会被并入同一分组（前提是中间没有其它打断元素）。

> 说明：引用的最终格式（合并显示、加括号等）由排版阶段的 `CiteGroup` 显示规则处理，不在 realize 范围内；本实践只验证「分组收集」这一步。

#### 4.3.5 小练习与答案

**练习 1**：`@arrgh @netwok` 中两个引用之间的 `SpaceElem`，为什么不会打断 CITES 分组？
> **答案**：因为 `CITES.effect(SpaceElem) == Inner`。空格作为 `Inner` 元素并入分组，于是两个 `CiteElem` 连同中间的空格留在同一个 `CiteGroup` 里。只有 `Interrupt`（如 `ParbreakElem` 或其它块级元素）才会打断。

**练习 2**：`finish_cites` 为什么直接用 `elems[0].1` 当 trunk，而不像 `finish_list_like` 那样调 `StyleChain::trunk`？
> **答案**：连续的 `CiteElem` 处于同一段行内样式下，样式链本就相同，求「公共前缀」等价于取其中任意一条；直接取第一个最省事。而且引用不做 per-item 的 suffix 拆分（所有子项共用 trunk），所以也没有算前缀的必要。

**练习 3**：`CITES` 与 `LIST` 的 priority 都是 2。若一段内容里同时出现引用和列表项（`@x` 紧跟 `- item`），会发生什么？
> **答案**：`@x`（`CiteElem`）的 `matching` 是 CITES，`- item`（`ListItem`）的 `matching` 是 LIST。当 `ListItem` 到来而栈顶是 CITES 时，`matching.priority (2)` 不严格大于 `active.priority (2)`，不会嵌套；而对 CITES 求 `effect(ListItem) == Interrupt`，于是先收尾 CITES 分组，再启动 LIST 分组。两者不会粘在一起。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「列表诞生全链路」追踪。

**任务**：用下面这个文档（示例代码）作为输入，完整解释一个混合了逐项样式的宽松列表是如何变成 `ListElem` 的：

```typst
#set page(width: 240pt)
#set list(marker: [--])

- One _italic_ item

- Another item
```

**要求你讲清下面这条链路上的每一跳**：

1. **启动**：第一个 `ListItem("One italic item")` 为何能启动 LIST 分组？此时若栈顶是 PAR（priority 1），priority 判定如何让它嵌套进 PAR？（参考 4.1.2、4.1.5 练习 2）
2. **保留宽松**：两项之间的空行产生 `ParbreakElem`，它为何不把列表拆开？它以什么 `effect` 留在分组里？（参考 4.1.2）
3. **收尾**：列表后面没有更多内容时，`finish_grouping` 如何在调 `finish_list_like` 之前剥离 tag（本例无 label，`tags` 为空）？（参考 4.1.3）
4. **判 tight**：`finish_list_like` 看到的 `elems` 里含不含 `ParbreakElem`？`tight` 最终是 `true` 还是 `false`？（参考 4.2.3）
5. **拆样式**：`#set list(marker: [--])` 是列表级样式，会进 trunk 还是某个 item 的 suffix？两项的 `_italic_` 差异又去哪了？（参考 4.2.3）
6. **回喂**：`T::create(children, tight)` 经 `s.store` 与 `visit(s, elem, trunk)` 后，`ListElem` 最终落到 sink 的哪一步？（参考 4.2.3）

**交付物**：一张标注了 `tight`、trunk（如 `#set list(marker: [--])` 与页面级样式）、各 item 的 local suffix、最终 `ListElem(children, tight)` 结构的示意图（手绘即可），并能指出每一步对应的源码行号。

> 提示：`#set list(...)` 作用于列表本身，属 trunk；`_italic_` 只在第一项内，是该项 body 的局部内容差异。具体取值待本地验证。

## 6. 本讲小结

- `list_like_grouping::<T>()` 是一个 `const fn` 泛型，借助 `ListLike`（`type Item` + `create`）与 `ListItemLike`（`styled`）两个 trait，用同一份代码生成 `LIST`/`ENUM`/`TERMS` 三条结构相同的规则。
- 三条规则的 `effect`：`T::Item` → `Trigger`、`SpaceElem`/`ParbreakElem` → `Inner`、其它 → `Interrupt`；`priority: 2`、`tags: false`。**`ParbreakElem` 作为 `Inner`** 是宽松列表能在同一分组内存活的关键。
- `finish_list_like` 做两件事：(1) `tight = !elems.iter().any(|c.is::<ParbreakElem>())` 判紧凑度；(2) 用 `StyleChain::trunk`（仅取 item 样式链）算公共前缀提到列表外，每个 item 用 `suffix(trunk_depth)` 折进各自 body。
- `CITES` 规则把连续 `CiteElem` 收成 `CiteGroup`，`ParbreakElem` 对它是 `Interrupt`（引用无紧凑概念）；`finish_cites` 最简——直接以 `elems[0].1` 为 trunk、克隆所有 `CiteElem` 为 children，不做 tight、不做 suffix 拆分。
- 列表与引用都设 `tags: false`：`finish_grouping` 会先把组内 `TagElem` 剥离压实，收尾后再单独 `visit`，使可定位元素的标签不被吞进列表/引用 body，保证内省正确。

## 7. 下一步学习建议

- **u2-l10（文本分组与正则 show 规则）**：去看 priority 3 的 `TEXTUAL` 规则如何跨多个文本元素做正则匹配，理解它与列表/引用（priority 2）在同一段落里如何分层嵌套。
- **u2-l11（空格折叠 spaces.rs）**：精读 `collapse_spaces` 与 `SpaceState` 四态机。注意列表/引用分组**不**自行折叠空格（`finish_list_like`/`finish_cites` 都没调 `collapse_spaces`），空格折叠由更外层的 PAR 收尾负责——这正是为什么 `SpaceElem` 能安全地作为 `Inner` 混在列表分组里。
- **u3-l1（标签与内省）**：深入 `tags: false` 的另一面——`TagElem` 如何在 `finish_grouping` 里被剥离、重访，以及 start/end tag 跨分组边界的纳入逻辑。
- 若你想确认 `tight` 最终如何影响排版（如 `list.spacing` 在 tight/non-tight 下的默认值差异），可阅读 `crates/typst-library/src/model/list.rs` 里 `tight`、`spacing` 字段的文档与 layout 实现。
