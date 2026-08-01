# 分组生命周期：启动、完成、中断

## 1. 本讲目标

在 [u2-l6](u2-l6-grouping-rule-framework.md) 中，我们认识了「分组」机制的数据骨架——`GroupingRule` 结构体、`GroupingEffect` 四态、四张静态规则表。但那份讲义通篇只回答了「分组**是什么**、**靠什么配置**」，刻意把「一个分组**怎么活、怎么死**」留到了本讲。

本讲就来补上这条动态的另一半：**分组的生命周期**。一个分组从被某个 `Trigger` 元素「点燃」而入栈，到不断吸纳后续元素而「长大」，再到被 `Interrupt` 元素、更高优先级规则、样式中断或流末尾「触发收尾」，最后被某个 `finish_*` 函数拆解、打包成一个新元素（如 `ParElem`）重新喂回流水线——这是一条完整的、有状态的时间线。

学完本讲，你应该能够：

- 说清 `visit_grouping_rules` 如何在「启动新分组 / 继续旧分组 / 结束旧分组」三种动作之间做调度，以及它内部那道 **512 次防死循环守卫**守护的是什么。
- 描述 `finish_innermost_grouping` 在分组「含中性元素」时，如何用 `group_by_key` 把混杂的内容**切片**成若干段子序列，再对每一段分别收尾。
- 理解 `finish_grouping` 的两件精细活：**尾部裁剪**（trim 掉不能开团的边缘元素）与 **tag 边界调整**（让 `start`/`end` 标签在分组裁剪时不被腰斩）。
- 区分三组收尾函数 `finish` / `finish_interrupted` / `finish_grouping_while` 的触发场景，并解释「maximum grouping depth exceeded」报错在什么情况下会被触发。

本讲只讲**生命周期的骨架与收尾公用逻辑**；六条规则各自的 `finish` 函数（`finish_par` / `finish_textual` / `finish_list_like` / `finish_cites`）内部如何具体打包，留待 [u2-l8](u2-l8-paragraph-grouping.md) 至 [u2-l11](u2-l11-space-collapsing.md)。

## 2. 前置知识

进入本讲前，请确认你已经建立以下直觉（前几讲已讲过，这里只做最简回顾）：

- **`Pair` 与 `sink`**：`realize()` 的输出是 `Vec<Pair<'a>>`，`Pair = (&'a Content, StyleChain<'a>)`（见 [u1-l2](u1-l2-realize-entrypoint-types.md)）。`State.sink` 就是这个输出清单在构建过程中的可变容器；分组「攒元素」就是往 `sink` 里 `push`，分组「收尾」就是从 `sink` 里取一段出来打包。
- **`groupings` 栈**：`State.groupings` 是容量为 `MAX_GROUP_NESTING = 3` 的 `ArrayVec`，保存「正在进行中」的分组（见 [u2-l1](u2-l1-state-and-grouping-stack.md)）。栈顶是最内层分组。
- **`GroupingRule` 五字段**：`priority`（优先级，仅 `{1,2,3}`）/ `tags`（是否自管标签）/ `effect`（判定元素与分组关系）/ `interrupt`（样式是否中断分组）/ `finish`（收尾打包函数）（见 [u2-l6](u2-l6-grouping-rule-framework.md)）。
- **`GroupingEffect` 四态**：`Trigger`（点燃/参与团）/ `Inner`（只能在团内部、不能在边缘）/ `Neutral`（可穿插、不触发收尾）/ `Interrupt`（终止团）。本讲会大量用到这四态在调度中的语义。
- **`Grouped` 是收尾时的「视图」**：收尾函数（如 `finish_par`）接收一个 `Grouped { s, start }`，它只是「从 `sink` 的 `start` 下标起到末尾」这一段的借用视图，配合 `end()` 方法把这段截掉、把状态交还（见下文 4.3）。

> 术语提示：本讲会频繁出现「收尾（finish）」一词，它指**把已经攒好的一段元素交给规则的 `finish` 函数打包成一个新元素、并重新喂回 `visit()`** 这个动作，而不是「函数返回」。一个分组的「收尾」恰恰是新内容「出生」的时刻。

## 3. 本讲源码地图

本讲只涉及一个源文件，但覆盖了其中最密集的一段调度与收尾逻辑：

| 文件 | 本讲关注的内容 |
|------|---------------|
| `crates/typst-realize/src/lib.rs` | `Grouping` 运行时结构体、`Grouped` 视图及其 `end()` 方法、`visit_grouping_rules`（调度主循环）、`finish_innermost_grouping`（含 neutral 分段）、`finish_grouping`（尾部裁剪 + tag 边界）、`finish` / `finish_interrupted` / `finish_grouping_while`（三组收尾入口与 512 守卫）、`tag_set` / `to_tag` 辅助函数 |

粗略地说，本讲围绕 `lib.rs` 的两段展开：第 149–169 行（运行时数据结构）、第 694–999 行（调度与全部收尾逻辑）。

## 4. 核心概念与源码讲解

### 4.1 `visit_grouping_rules`：分组的启动、继续与结束

#### 4.1.1 概念说明

`visit` 流水线的第 6 道关卡（见 [u1-l3](u1-l3-visit-dispatch-overview.md)）就是 `visit_grouping_rules`。它是分组机制的**唯一调度入口**：每一个未被前面关卡截胡的元素，都会来到这里，由它决定该元素是「点燃一个新分组」「并入当前分组」还是「终结当前分组」。

它要同时回答三个问题，而且回答的**顺序**很关键：

1. **这个元素能点燃哪条新规则吗？** —— 在当前规则表里找第一条 `effect(content) == Trigger` 的规则，记作 `matching`。
2. **它能不能并入已经在跑的分组？** —— 从最内层（栈顶）分组往外看。
3. **如果不能并入，是不是该先把旧分组收尾、腾出地方？**

这三步之所以有先后，是因为分组栈遵循一条铁律（[u2-l6](u2-l6-grouping-rule-framework.md) 已建立）：**只有优先级严格更高的规则才能嵌套进更内层；同级或更低则要先收尾外层**。用优先级数值表达就是：

\[ \text{新规则可嵌套进当前分组} \iff \text{priority}_{\text{新}} > \text{priority}_{\text{当前}} \]

#### 4.1.2 核心流程

`visit_grouping_rules` 的判定流程可以用下面这段伪代码概括：

```text
fn visit_grouping_rules(content):
    matching = rules 表里第一条 effect(content) == Trigger 的规则

    i = 0
    while 栈顶还有活跃分组 active:
        # (A) 嵌套判定：matching 优先级严格更高 → 跳出 while，去启动新分组
        if matching 存在 且 matching.priority > active.priority:
            break

        # (B) 并入判定：元素对 active 不是 Interrupt，且 active 未被标记 interrupted
        effect = active.effect(content)
        if not active.interrupted and effect != Interrupt:
            active.contains_neutral |= (effect == Neutral)
            sink.push(content)          # 并入当前分组
            return true

        # (C) 收尾判定：既不能嵌套也不能并入 → 收尾最内层分组，继续看下一层
        finish_innermost_grouping()
        i += 1
        if i > 512:
            bail "maximum grouping depth exceeded"

    # (D) 启动新分组：matching 命中且栈已腾好位置（或栈空）
    if let Some(rule) = matching:
        start = sink.len()
        groupings.push(Grouping { start, rule, interrupted: false, contains_neutral: false })
        sink.push(content)              # 首个元素入团
        return true

    return false                        # 没有任何规则匹配，交还 visit 继续往后走
```

三个动作对应代码里的三处：(A) 嵌套判定 → (C) 收尾循环 → (D) 启动新分组。注意 (B) 的并入是「就地 push 后立即返回」，是最常见的快路径。

#### 4.1.3 源码精读

先看入栈前找「点燃规则」的那一行，以及整个函数的边界：

- [crates/typst-realize/src/lib.rs:696-749](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L696-L749) —— `visit_grouping_rules` 全貌。`matching` 用 `find` 取规则表里**第一条** `effect == Trigger` 的规则（规则表本身按优先级从高到低排列，所以「第一条」就是「最高优先级的那条」）。

嵌套判定（伪代码 A）：

- [crates/typst-realize/src/lib.rs:710-712](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L710-L712) —— 若 `matching` 的优先级严格高于栈顶分组，就 `break` 跳出 while，把启动新分组的工作留给后面的 (D)。这正是「严格更高才嵌套」的体现。

并入判定（伪代码 B）：

- [crates/typst-realize/src/lib.rs:715-720](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L715-L720) —— 只要 `active` 没被标记 `interrupted`、且元素对该规则不是 `Interrupt`，就把元素 push 进 `sink`（落在该分组的 `[start..]` 区间内），并用 `|=` 累计 `contains_neutral`。这是分组的「长大」。

收尾判定与 512 守卫（伪代码 C）：

- [crates/typst-realize/src/lib.rs:722-732](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L722-L732) —— 收尾最内层分组，并把循环计数 `i` 与 512 比较。注意 `i` 统计的是「**为了安顿这一个元素而连续收尾了多少个分组**」，正常情况下绝不会接近 512；它真正防的是「show 规则产出被分组规则匹配的内容、分组收尾又触发 show 规则」的死循环（注释里解释得很清楚）。这与 4.4 里 `finish_grouping_while` 的 512 守卫是**两道不同的防线**，一个守「单元素触发的连续收尾」，一个守「收尾产生新内容后的迭代收尾」。

启动新分组（伪代码 D）：

- [crates/typst-realize/src/lib.rs:736-746](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L736-L746) —— 记下 `start = sink.len()` 作为分组在 `sink` 中的起点，`push` 一个全新的 `Grouping`（四个字段：`start` / `rule` / `interrupted=false` / `contains_neutral=false`），再把点燃元素本身 push 进 `sink`。`Grouping` 结构体定义见 [crates/typst-realize/src/lib.rs:150-161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L150-L161)。

> 关键不变量：`Grouping.start` 永远指向「这个分组攒的元素在 `sink` 中的起点」。只要分组还活着，`sink[start..]` 就是它已收集的全部元素；分组一旦收尾，这段就会被取走打包。

#### 4.1.4 代码实践

**目标**：用日志验证三种动作（启动 / 并入 / 收尾）在一次 `realize` 中的发生顺序与次数。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:718](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L718)（并入 push 后）加：
   ```rust
   eprintln!("[grouping] 并入 {:?} 到 {:?} 分组", content.elem(), active.rule.priority);
   ```
2. 在第 738 行 `s.groupings.push(...)` 之后（启动新分组处）加：
   ```rust
   eprintln!("[grouping] 启动新分组 priority={} 由 {:?} 点燃", rule.priority, content.elem());
   ```
3. 在第 722 行 `finish_innermost_grouping(s)?;` 之前加：
   ```rust
   eprintln!("[grouping] 收尾 priority={} 因为遇到 {:?}", active.rule.priority, content.elem());
   ```
4. 准备一个最小 Typst 文档 `doc.typ`：
   ```typst
   第一段文字。
   - 列表项一
   - 列表项二

   第二段文字。
   ```
5. 用 `cargo build -p typst`（或在仓库根目录用项目自带方式）编译后运行一次排版。

**需要观察的现象**：日志里应出现「启动 PAR（priority=1）」「收尾 PAR」「启动 LIST（priority=2）」「收尾 LIST」等交替行。

**预期结果**：文本点燃 `PAR` 分组；遇到列表项时，`LIST`（priority=2）严格高于 `PAR`（priority=1），于是**嵌套**（先不收尾 PAR），列表项被并入 LIST；列表结束、第二段文字到来时，因列表项不再是 LIST 的 trigger，LIST 被收尾，PAR 继续。具体日志行数与顺序**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若 `matching` 的优先级**等于**（而非高于）栈顶分组的优先级，会发生什么？是新嵌套还是先收尾？

**答案**：先收尾。判定用的是 `>`（严格大于）——见 [第 710 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L710)。同级不满足嵌套条件，于是落入 (C) 收尾最内层分组，循环再看下一层；直到栈空或遇到更低优先级分组时，才在 (D) 启动这个同级新分组。这就是 [u2-l6](u2-l6-grouping-rule-framework.md) 所说的「同级则抢占」。

**练习 2**：为什么 (B) 的并入判定里要额外检查 `!active.interrupted`？一个分组什么时候会被标记 `interrupted`？

**答案**：`interrupted` 标志表示「这个分组已被样式打断、但因为可能因『全行内』而被忽略，所以暂时不真正收尾」（见 4.4 的 `finish_interrupted` 与 `is_fully_inline_or_neutral`）。一旦被打上这个标记，后续元素就**不能再并入**它（即便 effect 不是 Interrupt），必须走收尾路径。它只对 `PAR` 分组有意义。

---

### 4.2 `finish_innermost_grouping`：neutral 元素的分段

#### 4.2.1 概念说明

当某个分组走到尽头（被 4.1 的 (C) 收尾、或被 4.4 的收尾入口调用），实际干活的就是 `finish_innermost_grouping`。它的名字说明职责：**收尾栈顶（最内层）那一个分组**。

它有两种走法，分水岭是分组攒的内容里**有没有中性（Neutral）元素**：

- **没有 neutral**（`contains_neutral == false`）：简单——直接把 `sink[start..]` 整段交给 `finish_grouping` 打包。
- **有 neutral**：复杂——必须先把 neutral 元素「剔出来」，把剩下的非 neutral 元素按连续段切成若干「子分组」，**每一段各自单独收尾**。

为什么 neutral 要单独处理？回顾 [u2-l6](u2-l6-grouping-rule-framework.md) 对 `Neutral` 的定义：neutral 元素「可以与团内元素穿插，但不会触发收尾」。它存在的全部意义，就是**让 HTML 里 inline 与 block 元素混排成为可能**。当这样一团「文本 + 内联 + 块级 HTML」混排的内容最终要收尾时，typst 不能把它揉成一个段落，而要切成「两块 block 之间那段 inline 内容 → 一个段落」「那块 block → 单独处理」「下一段 inline → 又一个段落」。

> 关键事实：在 `PAR` 规则里，`effect` 对 `HtmlElem` 的判定是 `should_group_into_pars(tag)` 为真 → `Trigger`，为假 → `Neutral`。而 `should_group_into_pars = is_phrasing_content(tag) && display != None`（见 `crates/typst-html/src/tag.rs`）。也就是说：**phrasing content（`span`/`em`/`b`/`code` 等内联）是 Trigger，会并入段落；块级元素（`div`/`p`/`blockquote` 等）是 Neutral，会被分段剔出**。

#### 4.2.2 核心流程

`finish_innermost_grouping` 的 `contains_neutral` 分支用 `group_by_key` 把已攒元素按「是不是 neutral」切成连续的同质段，再逐段处理：

```text
fn finish_innermost_grouping():
    Grouping { start, rule, contains_neutral, .. } = groupings.pop()   # 取出并出栈

    if contains_neutral:
        elems = 复制 sink[start..] 到 arena（store_slice）
        sink.truncate(start)                          # 先清空这段
        for (is_neutral, slice) in elems.group_by_key(effect == Neutral):
            if is_neutral:
                # neutral 段：不属于任何团，直接逐个 visit 回流水线
                for (content, styles) in slice: visit(content, styles)
            else:
                # 非 neutral 段：但开头可能混着不能开团的 Inner 元素，先 trim 掉
                trimmed = slice.trim_start_matches(effect != Trigger)
                把被 trim 掉的开头元素逐个 visit 回流水线
                if trimmed 非空:
                    start' = sink.len()
                    sink.extend(trimmed)
                    finish_grouping(rule, start')     # 这一段单独收尾
    else:
        finish_grouping(rule, start)                  # 整段直接收尾
```

注意三个细节：(1) 用 `store_slice` 把待处理内容复制进 arena，是为腾出 `sink` 给后续 `visit` / `finish_grouping` 写新内容（它们会往 `sink` 里 push）；(2) 非 neutral 段开头要 `trim_start_matches` 掉「非 Trigger」元素（即 `Inner` 元素，如 `SpaceElem`），因为 `finish_grouping` 只负责裁剪**尾部**的非 trigger，不管头部；(3) 每一段非 neutral 内容都各自调一次 `finish_grouping`，于是「文本 块 文本」会被切成两个段落。

#### 4.2.3 源码精读

- [crates/typst-realize/src/lib.rs:853-894](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L853-L894) —— `finish_innermost_grouping` 全貌。

出栈与分支：

- [crates/typst-realize/src/lib.rs:855-856](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L855-L856) —— `s.groupings.pop().unwrap()` 取出最内层分组（`unwrap` 安全，因为只有栈非空时才会被调到）；`..` 忽略 `interrupted` 字段。

neutral 分段的复制与清空：

- [crates/typst-realize/src/lib.rs:859-860](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L859-L860) —— `store_slice` 把 `sink[start..]` 复制进 bump arena（见 [u3-l3](u3-l3-lifetimes-and-arenas.md) 对 arena 的讲解），然后 `truncate(start)` 把这段从 `sink` 抹掉，给后续写入让位。

按 neutral 切片：

- [crates/typst-realize/src/lib.rs:861-863](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L861-L863) —— `group_by_key` 按 `(rule.effect)(c) == GroupingEffect::Neutral` 把相邻同质元素归为一段，返回 `(is_neutral, slice)` 迭代器。

neutral 段直接放行：

- [crates/typst-realize/src/lib.rs:864-867](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L864-L867) —— neutral 元素（块级 HTML）不属于段落，逐个 `visit` 回流水线，让它们各自走自己的 `visit` 关卡（它们通常会被 `visit_show_rules` 处理或直接落进 `sink`）。

非 neutral 段的头部裁剪：

- [crates/typst-realize/src/lib.rs:873-879](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L873-L879) —— `trim_start_matches` 砍掉开头连续的「非 Trigger」元素（如段首空格 `SpaceElem`，它们是 `Inner`），被砍掉的部分逐个 `visit` 回去；剩下的 `trimmed` 才是真正能开团的 trigger 元素。

非 neutral 段单独收尾：

- [crates/typst-realize/src/lib.rs:882-887](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L882-L887) —— 只有 `trimmed` 非空时，才把它写回 `sink` 末尾并以新的 `start'` 调 `finish_grouping`。注释点明：若一段全是 Inner 元素和 tag（没有 trigger），就根本不收尾、直接放行。`contains_neutral == false` 的简单走法见 [第 891-893 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L891-L893)。

#### 4.2.4 代码实践

**目标**：构造一段 inline 与 block 混排的 HTML 内容，在 neutral 分段处加日志，观察子分组如何被切分并分别收尾。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:861](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L861) 的 `for` 循环体内最前面加：
   ```rust
   eprintln!(
       "[neutral-seg] is_neutral={is_neutral}, 段内元素类型: {:?}",
       slice.iter().map(|(c, _)| c.elem()).collect::<Vec<_>>()
   );
   ```
2. 准备一个面向 HTML 导出的最小文档 `doc.typ`（块级 `html.div` 会成为 Neutral，文本与内联 `html.em` 成为 Trigger）：
   ```typst
   #html.div[
     前面一段文字 #html.em[强调] 接着写。
     #html.div[夹在中间的一块。]
     后面一段文字。
   ]
   ```
3. 用 HTML 目标编译（如 `typst compile doc.typ doc.html --format html`，具体子命令以本仓库 CLI 为准）。

**需要观察的现象**：日志里应能看到 `contains_neutral` 分支被命中，且 `group_by_key` 把内容切成形如 `[文本, em]`（非 neutral）→ `[div]`（neutral）→ `[文本]`（非 neutral）的若干段；每段非 neutral 内容随后各自触发一次 `finish_grouping`，最终产出多个 `ParElem`。

**预期结果**：中间的块级 `html.div` 被作为 neutral 段剔出并单独 `visit`；它前后的 inline 内容分别收尾成各自的段落。精确的日志条目与段数**待本地验证**（取决于 `html.div` 子内容如何流入父级的 PAR 分组）。

> 若一时无法触发 HTML 导出，可退而求其次：在 `contains_neutral` 分支入口（第 856 行）加一条 `eprintln!`，先确认普通文档（无 HTML）**永不**命中此分支——因为只有 `PAR` 规则对 `HtmlElem` 才会产生 `Neutral`，纯 Typst 文档的元素只会是 Trigger/Inner/Interrupt。

#### 4.2.5 小练习与答案

**练习 1**：为什么 non-neutral 段要用 `trim_start_matches` 砍头部，而 `finish_grouping`（4.3）只负责砍尾部？

**答案**：因为正常情况下，一个分组是由一个 `Trigger` 元素**点燃**才启动的（见 4.1 的 (D)），所以团的开头天然就是 trigger，不需要砍头部。但 neutral 分段切出来的子段，其开头可能是上一个 neutral 元素之后紧跟的 `Inner` 元素（如空格），这些不能开团，所以 `finish_innermost_grouping` 要在交给 `finish_grouping` 之前先把它们 trim 掉并逐个放行（[第 873-879 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L873-L879)）。尾部裁剪则是所有分组的共性，统一放在 `finish_grouping` 里。

**练习 2**：若一个 neutral 分段切出的某段非 neutral 内容 `trimmed` 为空（整段都是 Inner/tag），会发生什么？

**答案**：什么都不打包。第 882 行的 `if !trimmed.is_empty()` 守卫住了——这种情况只把开头那些非 trigger 元素逐个 `visit` 回流水线，不调 `finish_grouping`，避免凭空造出一个空段落。

---

### 4.3 `finish_grouping`：尾部裁剪与 tag 边界

#### 4.3.1 概念说明

`finish_grouping` 是所有收尾路径的**共同终点**（无论走 4.2 的哪条分支，最后都汇聚到这里）。它接收一个 `rule` 和起始下标 `start`，负责把 `sink[start..]` 这段内容**收拾干净**后交给规则的 `finish` 函数打包。它干两件精细活：

1. **尾部裁剪（trim）**：把团**末尾**那些不是 `Trigger` 的元素（如段尾空格 `SpaceElem`，它们是 `Inner`）剪掉——它们不该算进打包结果。
2. **tag 边界调整**：如果规则的 `tags == true`（如 `PAR`、`TEXTUAL`），还要处理内省用的 `Tag` 元素（`Tag::Start`/`Tag::End`，由 [u2-l4](u2-l4-prepare-first-visit.md) 的 `prepare` 生成）——确保跨越分组边界的标签对不被腰斩。

理解 tag 边界调整，需要先回忆 `Tag` 是什么：locatable 元素在 `prepare` 时会被一对 `Tag::Start` / `Tag::End` 三明治式夹住（见 [u2-l4](u2-l4-prepare-first-visit.md)、[u3-l1](u3-l1-tags-and-introspection.md)），它们以 `TagElem` 的形式混在 `sink` 里，供排版后内省（query/ref/PDF 标注）定位元素的范围。当一个段落分组被裁剪时，某个元素的 `Start` 标签可能落在段落内、`End` 标签落在段落外——`finish_grouping` 要保证配对的标签要么整体纳入、要么整体排除。

#### 4.3.2 核心流程

`finish_grouping` 的流程可以拆成五步：

```text
fn finish_grouping(rule, start):
    # 步骤 1：尾部裁剪。剪掉末尾连续的非 Trigger 元素，得到 [start, end)
    trimmed = sink[start..].trim_end_matches(effect != Trigger)
    end = start + trimmed.len()

    # 步骤 2（仅当 rule.tags）：tag 边界调整
    if rule.tags:
        # 2a. PAR 专享：把紧贴 end 之后、会被空格折叠吃掉的 SpaceElem 提前移走
        if rule is PAR: extract_if(end.., is SpaceElem) 清空
        # 2b. 收集三个区间的标签 location 集合
        before = tag_set(sink[..start])      # 团之前
        within = tag_set(sink[start..end])   # 团之内
        after  = tag_set(sink[end..])        # 团之后
        # 2c. 向左扩 start：团前的 Start 标签，若其 End 落在 within/after，则纳入
        for k in (..start).rev(): 若是 TagElem 且 location ∈ within ∪ after: start = k
        # 2d. 向右扩 end：团后的 End 标签，若其 Start 落在 within/before，则纳入
        for k in (end..): 若是 TagElem 且 location ∈ within ∪ before: end = k + 1

    # 步骤 3：把 end 之后的尾巴暂存（tail），truncate 到 end
    tail = store_slice(sink[end..]); sink.truncate(end)

    # 步骤 4（仅当 !rule.tags）：从 [start,end) 里抽出所有 TagElem 另存（tags），原地压缩
    if !rule.tags: 把 TagElem 挑出来放进 tags，剩余元素前移压实

    # 步骤 5：执行规则收尾函数（如 finish_par），它会 end() 截掉这段、造新元素并 visit 回去
    (rule.finish)(Grouped { s, start })

    # 步骤 6：把暂存的 tags 与 tail 重新 visit 回流水线
    for (content, styles) in tags.chain(tail): visit(content, styles)
```

`Grouped` 视图与它的 `end()` 方法是步骤 5 的关键：`end()` 会 `truncate(start)` 把这段从 `sink` 抹掉并把 `State` 交还给收尾函数，于是 `finish_par` 等函数可以在干净的 `sink` 末尾 `push` 新造的 `ParElem`。

#### 4.3.3 源码精读

- [crates/typst-realize/src/lib.rs:898-981](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L898-L981) —— `finish_grouping` 全貌。

步骤 1 尾部裁剪：

- [crates/typst-realize/src/lib.rs:905-907](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L905-L907) —— `trim_end_matches` 砍掉末尾连续的非 trigger 元素，`end = start + trimmed.len()`。注释说明：开头不需要在这里 trim，因为分组总是由 trigger 启动（neutral 分段的情况已在 4.2 处理）。

步骤 2 的 PAR 专享清理：

- [crates/typst-realize/src/lib.rs:922-924](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L922-L924) —— 仅当规则正是 `PAR`（用 `std::ptr::eq(rule, &PAR)` 判同一条静态规则）时，把紧贴 `end` 之后、反正会被空格折叠吃掉的 `SpaceElem` 用 `extract_if` 提前移走，避免它们干扰后续 tag 边界判定。

三个标签集合：

- [crates/typst-realize/src/lib.rs:927-930](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L927-L930) —— `tag_set` 分别收集团前（`..start`，反向 `map_while`）、团内（`start..end`）、团后（`end..`，正向 `map_while`）三段里**紧邻边界**的标签 location。`tag_set` 与 `to_tag` 定义见 [crates/typst-realize/src/lib.rs:985-999](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L985-L999)。`map_while`/`filter_map` 保证只取「紧贴边界」的连续标签段，遇到非标签元素即停。

向左/向右扩张边界：

- [crates/typst-realize/src/lib.rs:933-939](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L933-L939) —— 从 `start` 向左扫：团前的一个 `Start` 标签，若它的配对 `End` 落在团内或团后（`within ∪ after`），就把 `start` 左移把它纳入。遇到非 `TagElem` 即 `break`。
- [crates/typst-realize/src/lib.rs:942-948](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L942-L948) —— 从 `end` 向右扫：团后的一个 `End` 标签，若它的配对 `Start` 落在团内或团前（`within ∪ before`），就把 `end` 右移把它纳入。这两步合起来保证：**配对的标签不会被分组边界腰斩**。

暂存尾巴与抽标签：

- [crates/typst-realize/src/lib.rs:951-952](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L951-L952) —— 把 `end` 之后的元素（`tail`）暂存进 arena，再 `truncate(end)`，让 `[start, end)` 成为 `sink` 的末尾段。
- [crates/typst-realize/src/lib.rs:954-970](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L954-L970) —— 当 `!rule.tags`（如 `CITES`/`LIST`/`ENUM`/`TERMS`）时，把 `[start, end)` 里的 `TagElem` 挑出来另存到 `tags`，剩余元素用读/写双指针 `k` **原地压实**，再 `truncate(k)`。这样规则的 `finish` 函数就只看到纯净的业务元素。

执行收尾与回放：

- [crates/typst-realize/src/lib.rs:973](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L973) —— 调 `(rule.finish)(Grouped { s, start })`，例如 `finish_par` 会在其中调 `grouped.end()`（[crates/typst-realize/src/lib.rs:235-238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L235-L238)）截掉这段、造出 `ParElem` 并 `visit` 回流水线。
- [crates/typst-realize/src/lib.rs:976-978](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L976-L978) —— 把刚才挑出来的 `tags` 和暂存的 `tail` 依次 `visit` 回流水线。这一步至关重要：它保证被裁剪/剔出的元素不会丢失，而是重新进入 `visit` 去寻找自己的归宿（可能落入下一个分组或直接进 `sink`）。

#### 4.3.4 代码实践

**目标**：观察尾部裁剪与 tag 边界调整如何改变 `[start, end)` 的范围。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:907](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L907)（尾部裁剪后）加：
   ```rust
   eprintln!("[finish_grouping] 裁剪后 start={start} end={end} tags={}", rule.tags);
   ```
2. 在第 948 行（tag 边界调整结束后、暂存 tail 之前）加：
   ```rust
   eprintln!("[finish_grouping] tag 调整后 start={start} end={end}");
   ```
3. 准备一个带 label 的文档（label 会触发 `prepare` 分配 location 并生成 `Tag`，见 [u2-l4](u2-l4-prepare-first-visit.md)）：
   ```typst
   一些文字 <my-label> 带标签的内容 更多文字。
   ```
4. 编译并观察日志。

**需要观察的现象**：第一次打印的 `[start, end)` 是按「Trigger 元素」裁剪后的范围；若该范围内有标签跨边界，第二次打印的 `start`/`end` 会有所扩张。

**预期结果**：当某个带 label 元素的 `Start`/`End` 标签正好骑在段落边界上时，能看到 `start` 左移或 `end` 右移把标签纳入。具体的数值变化**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`rule.tags` 为 `true` 与 `false` 时，`finish_grouping` 对 `TagElem` 的处理有何不同？

**答案**：`tags == true`（`PAR`/`TEXTUAL`）时，标签被视为团内容的一部分：通过边界调整把配对标签纳入团内，并最终随团内容一起交给 `finish` 函数（如 `finish_par` 会把标签留在段落里，供后续内省）。`tags == false`（`CITES`/列表类）时，标签与团业务无关：在第 954–970 行把 `TagElem` 挑出来另存、原地压实，让 `finish` 只看到纯净元素，标签则在第 976–978 行被重新 `visit` 回流水线。

**练习 2**：步骤 3 为什么要先把 `tail` 暂存再 `truncate`，而不是直接处理 `[start, end)`？

**答案**：因为步骤 5 的 `(rule.finish)(...)` 会通过 `Grouped::end()` 调 `sink.truncate(start)`，把 `[start, ...]` 整段截掉。如果 `end` 之后还留着 `tail`，`end()` 会连 tail 一起截掉造成丢失。所以必须先把 tail 暂存到 arena，等收尾完成后再在第 976–978 行把 tail 重新 `visit` 回去。

---

### 4.4 `finish` / `finish_interrupted` / `finish_grouping_while`：收尾流水线与 512 守卫

#### 4.4.1 概念说明

前面三节讲的是「单个分组怎么收尾」。本节讲的是「**什么时候、为什么**要收尾」的三个触发入口，它们都最终落到 `finish_innermost_grouping`：

- **`finish`**：`realize()` 主流程在 `visit()` 结束后调用的**总收尾**（[第 71 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L71)）。它把栈里所有还在进行的分组一个个收掉，并处理 Fragment 的「全行内回退」优化。
- **`finish_interrupted`**：在 `visit_styled`（[u2-l5](u2-l5-styled-and-page-doc-styles.md)）里调用，**当遇到会中断分组的新样式时**，先收掉被中断的分组。它在带样式元素的子内容访问**前后各调一次**。
- **`finish_grouping_while`**：上面两个入口的**公共驱动器**。它接收一个谓词 `f`，「只要 `f(s)` 为真就持续收尾最内层分组」，并带一道 512 次的防死循环守卫。

三者的关系是：`finish` 和 `finish_interrupted` 都把「判定条件」包成闭包传给 `finish_grouping_while`，由后者驱动真正的 `finish_innermost_grouping`。

> 为什么需要 512 守卫？因为**收尾会产生新内容**：`finish_par` 造出的 `ParElem` 会重新 `visit`，可能又点燃新分组、又触发收尾。理论上 show 规则与分组规则若处于某种「均衡」，这个过程会无限循环。512 守卫就是这道兜底，超限即抛「maximum grouping depth exceeded」。注意它与 show 规则深度的 `MAX_SHOW_RULE_DEPTH = 64`（见 [u2-l2](u2-l2-show-rule-application.md)）是**互补的两道防线**：一个管「show 跨规则递归深度」，一个管「show↔grouping 循环迭代次数」。

#### 4.4.2 核心流程

`finish_grouping_while` 极简：

```text
fn finish_grouping_while(f):
    i = 0
    while f(s):                      # 只要谓词为真
        finish_innermost_grouping()  # 收掉最内层
        i += 1
        if i > 512:
            bail "maximum grouping depth exceeded"
```

`finish` 的闭包优先处理 Fragment 回退：

```text
fn finish():
    finish_grouping_while(|s|:
        if 这是 Fragment 且内容「全行内或全 neutral」(is_fully_inline_or_neutral):
            把 FragmentKind 改写成 Inline   # 不强制包成段落
            groupings.pop()                # 丢弃这个 PAR 分组
            collapse_spaces(sink, 0)
            return false                   # 停止收尾
        else:
            return 栈非空                  # 继续收掉所有活跃分组
    )
    # Par/Math 模式下，顶层空格也需要折叠
    if kind 是 Par 或 Math: collapse_spaces(sink, 0)
```

`finish_interrupted` 遍历新样式涉及的元素类型，逐类收尾：

```text
fn finish_interrupted(local):
    last = None
    for elem in local 里出现的元素类型（去重）:
        finish_grouping_while(|s|:
            栈里是否有分组的 interrupt(elem) 为真？
            且 若内容「全行内」则只标记 interrupted=true 不真收尾（保留给 Fragment 回退）
        )
        last = elem
```

#### 4.4.3 源码精读

`finish_grouping_while` 及其 512 守卫：

- [crates/typst-realize/src/lib.rs:834-850](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L834-L850) —— 公共驱动器。注释明说「收尾可能产生新内容和新分组，理论上会持续一阵；为防止无限循环，记迭代数」。512 这个魔数与 `visit_grouping_rules` 里的 512（[第 724 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L724)）是两处独立的守卫。

`finish` 的总收尾与 Fragment 回退：

- [crates/typst-realize/src/lib.rs:788-810](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L810) —— `finish` 全貌。
- [crates/typst-realize/src/lib.rs:792-798](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L792-L798) —— Fragment 回退：若 `is_fully_inline_or_neutral(s)` 为真，把 `FragmentKind` 改写成 `Inline`、丢弃唯一的 PAR 分组、折叠空格，返回 `false` 让循环停下。判定函数见 [crates/typst-realize/src/lib.rs:1173-1186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1173-L1186)——它要求「恰好一个 PAR 分组、且它覆盖整个 sink（除前导 tag/neutral）」。
- [crates/typst-realize/src/lib.rs:805-807](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L805-L807) —— Par/Math 模式下，顶层（从下标 0 起）的空格也要折叠（空格折叠本身见 [u2-l11](u2-l11-space-collapsing.md)）。

`finish_interrupted` 的样式中断收尾：

- [crates/typst-realize/src/lib.rs:813-831](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L813-L831) —— 遍历 `local` 样式里出现的元素类型（用 `last` 去重），对每个类型 `elem` 用 `finish_grouping_while` 收掉所有「`interrupt(elem)` 为真」的分组。
- [crates/typst-realize/src/lib.rs:820-826](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L820-L826) —— 谓词：栈里是否存在某分组被这种元素类型中断；同时若内容「全行内」，则只把栈底分组标记 `interrupted = true` 而不真收尾（保留给 `finish` 的 Fragment 回退路径处理）。它的调用点在 `visit_styled`：[第 680 行与第 682 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L680-L682)，子内容访问前后各一次。

> 三处 512 守卫一览：(1) `visit_grouping_rules` 内的 [第 724 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L724)（单元素触发的连续收尾）；(2) `finish_grouping_while` 的 [第 845 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L845)（收尾产生新内容后的迭代收尾）。两处报错信息都是 `"maximum grouping depth exceeded"`。

#### 4.4.4 代码实践

**目标**：触发「maximum grouping depth exceeded」，观察 512 守卫如何兜底。

**操作步骤**：

1. 把 [crates/typst-realize/src/lib.rs:845](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L845) 的 `if i > 512` 临时改成 `if i > 8`（降低阈值便于观察），并在其上一行加：
   ```rust
   eprintln!("[guard] finish_grouping_while 迭代 i={i}");
   ```
2. 构造一个让 show 规则产出「会被分组规则匹配的内容」的循环文档，例如（示例代码，仅为构造 show↔grouping 循环的示意，具体能否触发取决于规则匹配关系）：
   ```typst
   #show par: it => [#it #it]
   循环。
   ```
3. 编译并观察日志。

**需要观察的现象**：若成功构造出循环，日志里 `i` 会从 1 逐次攀升，越过阈值后编译报错 `maximum grouping depth exceeded`。

**预期结果**：`i` 单调递增直至阈值；阈值改回 512 后，需更强的循环才能触发。能否触发、触发位置**待本地验证**——上面这条 show 规则未必恰好形成 show↔grouping 均衡，可能先撞上 `MAX_SHOW_RULE_DEPTH = 64` 的 show 深度防线（见 [u2-l2](u2-l2-show-rule-application.md)）。实践的价值在于对比这两道防线的触发先后。

> 实践结束后**务必把阈值改回 512**，并删除调试日志——本讲义不应改动源码最终状态。

#### 4.4.5 小练习与答案

**练习 1**：`finish` 的闭包里，为什么在 Fragment「全行内」时要 `pop()` 掉分组并返回 `false`，而不是继续收尾？

**答案**：因为 Fragment 的语义是「一段可能内联、可能成块的内容」。如果它全是行内元素（没有真正的块级内容），就不该被强制包成一个 `ParElem`，而应保持为内联片段——这正是把 `FragmentKind` 改写成 `Inline` 的意义（见 [u3-l4](u3-l4-realization-kinds-in-depth.md)）。此时那个唯一的 PAR 分组是「候选段落」，直接 `pop()` 丢弃、折叠空格、返回 `false` 停止收尾，让这些行内元素原样留在 `sink` 里输出。

**练习 2**：`finish_interrupted` 为什么用 `last` 对 `elem` 去重，而不是对每种样式都处理一次？

**答案**：因为同一个元素类型的 `set` 规则可能在一组 `Styles` 里出现多次（比如多条 `set text(...)`），但 `interrupt(elem)` 的判定只关心「**元素类型**」是否中断分组，与具体属性无关。重复处理同一类型只会做无用功，所以用 `last == Some(elem)` 跳过连续的同类型样式。

---

## 5. 综合实践

把本讲四节串起来，做一次完整的「分组生命周期」追踪。

**任务**：用一个混合了文本、列表、带 label 元素的文档，画出一次 `realize` 中分组栈的演化时间线。

**文档** `doc.typ`：

```typst
#set list(marker: [-])

首段文字 <lbl> 带标签。

- 项一
- 项二

尾段文字。
```

**操作**：

1. 在以下四处加 `eprintln!`（标签内注明发生点）：
   - [第 738 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L738) 后：打印「启动分组 priority=X，stack 深度={}」。
   - [第 718 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L718) 后：打印「并入 priority=X」。
   - [第 855 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L855) 后：打印「收尾 priority=X，contains_neutral={}」。
   - [第 973 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L973) 前：打印「执行 finisher，start={}」。
2. 编译运行，按日志时间顺序绘制分组栈深度变化曲线（横轴=日志序号，纵轴=`groupings.len()`）。

**预期观察**（基于源码推理，数值**待本地验证**）：

- 「首段文字」点燃 `PAR`（priority=1），栈深 1；带 label 的内容触发 `prepare` 生成 `Tag`（见 [u2-l4](u2-l4-prepare-first-visit.md)），`Tag` 随后并入 `PAR`。
- 列表项到来：`LIST`（priority=2）严格高于 `PAR`，**嵌套**，栈深升到 2；第二项并入 `LIST`。
- 「尾段文字」到来：列表项不再是 `LIST` 的 trigger，`LIST` 收尾（栈深回 1），`PAR` 继续吸纳尾段。
- `visit` 结束后 `finish()` 收掉剩下的 `PAR`（栈深回 0）。
- 全程 `contains_neutral` 应**始终为 false**（纯 Typst 文档无 `Neutral`，neutral 只来自块级 HTML）。

**思考题**：如果把文档改成 HTML 导出、在文本间插入 `#html.div[块]`，`contains_neutral` 会在哪个分组的收尾日志里变成 `true`？对应走 4.2 的哪条分支？（答案：会命中 4.2 的 `contains_neutral == true` 分支，块级 `div` 作为 neutral 段被剔出、前后文本各成一段。）

## 6. 本讲小结

- **`visit_grouping_rules` 是分组唯一调度入口**，按「嵌套判定（严格更高才嵌套）→ 并入判定（非 Interrupt 且未 interrupted）→ 收尾判定」三步处理每个元素，最后才启动新分组；它自带一道 512 守卫防止单元素触发连续收尾死循环。
- **`finish_innermost_grouping` 的分水岭是 `contains_neutral`**：无 neutral 直接整段收尾；有 neutral 则用 `group_by_key` 把混杂内容切成多段，neutral 段直接放行、非 neutral 段各自单独收尾——这正是 HTML inline/block 混排的基础。
- **`finish_grouping` 干两件精细活**：尾部裁剪（trim 掉不能开团的边缘 `Inner` 元素）与 tag 边界调整（用 before/within/after 三个 location 集合，把骑在分组边界上的配对 `Start`/`End` 标签整体纳入或排除，避免腰斩）。
- **`Grouped` 视图与 `end()` 是收尾的关键**：`end()` 截掉团对应的 `sink` 段并把状态交还，让收尾函数能在干净的末尾造出新元素 `visit` 回去；被裁剪/剔出的 tail 与 tag 也都重新 `visit` 回流水线，不会丢失。
- **三个收尾入口** `finish`（总收尾，含 Fragment 全行内回退）、`finish_interrupted`（样式中断，子内容访问前后各一次）、`finish_grouping_while`（公共驱动器）都汇聚到 `finish_innermost_grouping`，且都受 512 守卫保护。
- **「maximum grouping depth exceeded」**由 512 守卫触发，专门对付 show 规则与分组规则互相喂料形成的循环；它与 `MAX_SHOW_RULE_DEPTH = 64` 的 show 深度防线互补。

## 7. 下一步学习建议

本讲把分组生命周期的**骨架与公用收尾逻辑**讲完了，但每种分组「具体打包成什么」还没展开。建议按以下顺序继续：

- **[u2-l8](u2-l8-paragraph-grouping.md) 段落分组与 ParElem 构建**：精读 `finish_par` 与 `repack`，看本讲的 `finish_grouping` 是如何把行内元素折叠成 `ParElem` 的，以及 `is_fully_inline_or_neutral` 的回退细节。
- **[u2-l9](u2-l9-list-enum-terms-cites.md) 列表/枚举/术语与引用分组**：精读 `finish_list_like`（看 tight 判定与 trunk 样式提取）与 `finish_cites`，对应本讲提到的 `tags == false` 那类规则。
- **[u2-l10](u2-l10-textual-grouping-regex.md) 文本分组与正则 show 规则**：精读 `finish_textual`，它特殊在「收尾时可能不打包、而是把元素转交给 PAR 分组」，是本讲流程的一个变体。
- **[u3-l1](u3-l1-tags-and-introspection.md) 标签与内省**：深入理解本讲 4.3 反复出现的 `Tag` / `TagElem` 到底如何服务内省，回头再看 tag 边界调整会更通透。
