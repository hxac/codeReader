# 段落分组与 ParElem 构建

## 1. 本讲目标

本讲聚焦 typst-realize 里最常见的分组——**段落分组（PAR）**。学完后你应当能够：

- 说出 `PAR` 规则的 `effect` 函数把哪些元素判定为 `Trigger` / `Inner` / `Neutral` / `Interrupt`，从而理解「什么内容会被收进同一个段落」。
- 读懂 `finish_par` 如何把一段带样式的 pair 列表折叠、重新打包，最终生成一个 `ParElem` 并喂回 `visit`。
- 读懂 `repack` 如何从一堆 `(Content, StyleChain)` pair 中提取公共的 **trunk 样式链**，再把每个样式分组的 **suffix** 重新包成 `StyledElem`。
- 说清 `is_fully_inline_or_neutral` 在什么条件下让一个 `Fragment` 具现化「放弃生成段落」，转而把 `FragmentKind` 改写成 `Inline`。

本讲是 u2-l7（分组生命周期）的自然延续：u2-l7 讲完了分组从启动到收尾的通用骨架，本讲钻进 PAR 这条规则自己的 `finish` 收尾细节。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些在前几讲建立的概念（本讲直接复用，不再重复定义）：

- **realization（具现化）**：把任意 content 树递归套用样式与 show 规则，规整成扁平的、全部由后端已知元素组成的 pair 清单。入口是 `realize()`，输出是 `Vec<Pair<'a>>`，其中 `Pair<'a> = (&'a Content, StyleChain<'a>)`。（见 u1-l1、u1-l2）
- **`GroupingRule` 静态说明书**：含 `priority`、`tags`、`effect`、`interrupt`、`finish` 五个字段；`effect(content)` 返回 `GroupingEffect::{Trigger, Inner, Neutral, Interrupt}` 描述元素与本分组的关系。（见 u2-l6）
- **分组栈与优先级嵌套**：`State.groupings` 是容量为 `MAX_GROUP_NESTING = 3` 的栈；**新规则 priority 须严格高于栈顶才能嵌套其内**，因此 priority 数值越大越「内层」。六条规则的 priority 只有 `{1,2,3}` 三档。（见 u2-l1、u2-l7）
- **分组生命周期**：`visit_grouping_rules` 负责启动/继续/收尾；`finish_innermost_grouping` 按 `contains_neutral` 分水岭决定整段收尾还是切片收尾；`finish_grouping` 做尾部裁剪与 tag 边界调整，最后调用 `(rule.finish)(Grouped { s, start })`。（见 u2-l7）
- **`Grouped` 视图**：`get()` 取 `&sink[start..]`、`get_mut()` 取 `(&mut sink, start)`、`end()` 把 sink 截断到 `start` 并交还 `State`，供 `finish_*` 函数产出新元素后继续 `visit`。（见 u2-l7）

一个直觉铺垫：**为什么需要 repack？** 具现化一路把 content「拍扁」成 `(元素, 样式链)` 的 pair 列表，是为了方便分组、过滤、空格折叠。但 `ParElem` 的 `body` 字段是一个 `Content`，不是 pair 列表。所以收尾时必须把 pair 列表「重新打包」回一棵带样式的 content 树——这就是 `repack` 要做的事，它是 `finish_par` 的核心。

## 3. 本讲源码地图

本讲只涉及两个文件，外加一处 typst-library 的样式链辅助函数：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | PAR 规则定义、`finish_par`、`repack`、`is_fully_inline_or_neutral`，以及把文本喂进 PAR 的 `finish_textual` |
| `crates/typst-realize/src/spaces.rs` | `collapse_spaces` 原地空格折叠算法，被 `finish_par` 调用 |
| `crates/typst-library/src/foundations/styles.rs` | `StyleChain::trunk_from_pairs` / `suffix` / `links`，是 `repack` 的底层支撑 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**PAR 规则**、**finish_par / repack**、**is_fully_inline_or_neutral**。

### 4.1 PAR 规则：决定段落成员

#### 4.1.1 概念说明

`PAR` 是把「连续的行内（inline）元素」收拢成一个 `ParElem` 的分组规则。它是六条规则里 priority **最低（1）** 的一条，因此在分组栈里处于**最外层**——其它 priority 更高的规则（TEXTUAL=3、CITES/列表=2）都可以嵌套在 PAR 之内。换句话说，一个段落是容器，里面装着文本串、引用、列表等更内层的分组产物。

> 提醒：`PAR` 只出现在 `FLOW_RULES`（普通 Document / Fragment 具现化）中，**不在** `PAR_RULES` 里。因为当你用 `RealizationKind::Par` 去「具现化一个已有段落内部」时，绝不能再凭空套一层 `ParElem`。规则表的选择发生在入口处：
>
> [crates/typst-realize/src/lib.rs:L55-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L55-L61) —— 按 `kind` 选用 `BUNDLE/FLOW/PAR/MATH` 四张规则表，`Par` 选用 `PAR_RULES`（不含 `&PAR`）。

#### 4.1.2 核心流程

PAR 规则自身的定义如下，关键是它的 `effect` 闭包——它判定每一个流经的元素属于哪一档：

[crates/typst-realize/src/lib.rs:L1044-L1071](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1044-L1071) —— `PAR` 静态规则：`priority: 1`、`tags: true`、`finish: finish_par`。

`effect` 的判定可以用一张表概括：

| 元素 | `effect` | 含义 |
| --- | --- | --- |
| `TextElem` / `HElem` / `LinebreakElem` / `SmartQuoteElem` / `InlineElem` / `BoxElem` | `Trigger` | 行内元素，能**触发**并栖居段落 |
| `SpaceElem` | `Inner` | 空格只能在段落**内部**出现，不能在首尾 |
| `HtmlElem`（`should_group_into_pars` 为真） | `Trigger` | 某些 HTML 块级标签也算段落触发者 |
| `HtmlElem`（其余） | `Neutral` | 中性，可与块级内容交织而不打断段落（HTML 专用） |
| 其它一切（标题、列表、`ParElem` 自身……） | `Interrupt` | 块级元素，**打断**当前段落 |

此外：

- `interrupt` 字段为 `|elem| elem == ParElem::ELEM || elem == AlignElem::ELEM`：遇到 `set par(...)` 或 `set align(...)` 这类**样式**时，当前段落要收尾（这条由 `visit_styled` → `finish_interrupted` 触发，见 u2-l5 / u2-l7）。
- `tags: true`：PAR **自管标签**。这带来两个后果：(1) `finish_grouping` 不会替 PAR 剥离 tag，tag 会留在 sink 段里随内容一起进 `finish_par`；(2) `finish_grouping` 会对 PAR 跑一段专属的尾部空格清理（见 4.1.3）。

#### 4.1.3 源码精读

PAR 规则本体见上面的 L1044-L1071。这里补充两处「PAR 才有」的特殊代码。

**第一，段落往往不是由 `visit_grouping_rules` 直接启动，而是由 `finish_textual` 间接播种。** 看一个 `TextElem` 的走向：在 `FLOW_RULES` 里 `visit_grouping_rules` 用 `.find(...)` 找第一个 `effect == Trigger` 的规则，`TEXTUAL.effect(TextElem) == Trigger` 命中在前，所以文本先进 priority 更高的 TEXTUAL 分组。当 TEXTUAL 收尾、且没有正则匹配时，`finish_textual` 会把这些文本**透明地移交给一个 PAR 分组**（没有就新建一个）：

[crates/typst-realize/src/lib.rs:L1147-L1158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1147-L1158) —— 若当前没有任何分组、且规则表里含 `&PAR`，就 `push` 一个 PAR 分组，把文本就地并入。

也就是说，文本元素的真实旅程是 `TextElem` → TEXTUAL 分组（priority 3）→ `finish_textual` 无正则命中 → 落入 PAR 分组（priority 1）。这是理解「段落从哪来」的关键。

**第二，`finish_grouping` 对 PAR 做了一段专属优化。** 在裁剪完尾部非 Trigger 元素、计算 tag 边界之前，若发现当前规则正是 `&PAR`，会把段尾残余的 `SpaceElem` 提前剔除：

[crates/typst-realize/src/lib.rs:L922-L924](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L922-L924) —— 用 `extract_if` 把 `end` 之后的 `SpaceElem` 删掉，避免它们干扰 tag 边界匹配。

#### 4.1.4 代码实践

**实践目标**：验证「文本先进 TEXTUAL、再被移交 PAR」这一播种路径。

**操作步骤**（源码阅读型，无需编译）：

1. 打开 `crates/typst-realize/src/lib.rs`，定位 `TEXTUAL` 规则（L1018-L1041）与 `PAR` 规则（L1044-L1071）。
2. 对一个 `TextElem`，分别写出 `TEXTUAL.effect` 与 `PAR.effect` 的返回值，确认 `visit_grouping_rules` 里的 `.find(...)` 会先命中 TEXTUAL。
3. 阅读 `finish_textual`（L1130-L1161），跟踪「无正则命中 → `in_non_par_grouping` 判定 → `push` PAR 分组」这条路径。

**需要观察的现象**：文本从不直接触发 PAR，而是经 TEXTUAL 中转。

**预期结果**：`finish_textual` 末尾的 `if s.groupings.is_empty() && ... std::ptr::eq(rule, &PAR)` 分支是段落分组的真正诞生地。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PAR` 的 `priority` 是 1（最低），而不是像 TEXTUAL 那样是 3？
> **答案**：priority 越大越「内层」。段落是最外层的容器，里面的文本串（TEXTUAL）、引用（CITES）、列表（LIST/ENUM/TERMS）都要能嵌套其内，所以它们 priority 都必须严格大于 PAR，PAR 只能取最小值 1。

**练习 2**：一个 `BoxElem` 流经 `visit_grouping_rules` 时，`matching` 规则是谁？为什么？
> **答案**：是 `PAR`。因为 `TEXTUAL.effect(BoxElem) == Interrupt`、`CITES/LIST/ENUM/TERMS.effect(BoxElem) == Interrupt`，只有 `PAR.effect(BoxElem) == Trigger`，`.find(...)` 跳过前面几条后命中 PAR。所以 `BoxElem`（不像 `TextElem`）会**直接**启动/并入 PAR 分组。

---

### 4.2 finish_par 与 repack：把行内元素打包成 ParElem

#### 4.2.1 概念说明

当 PAR 分组收尾时，`finish_grouping` 调用 `(rule.finish)(Grouped { s, start })`，对 PAR 来说就是 `finish_par`。它的职责是把 sink 里 `[start..]` 这段「拍扁」的 pair 列表，重新组织成一个 `ParElem`，再把新元素喂回 `visit`（最终落入 sink 成为 well-known 元素）。

这里有一个核心张力：**具现化把样式「摊」到了每个 pair 上（`StyleChain`），但 `ParElem.body` 只是一个 `Content`，没有地方挂「整段公共样式」。** `finish_par` 通过 `repack` 解决它：抽取出所有 pair 共享的公共样式前缀（**trunk**），把 trunk 提到「外面」作为传给 `visit(ParElem, trunk)` 的样式；每个 pair 上**多出来**的局部样式（**suffix**）则重新包成 `StyledElem` 留在 `body` 内部。

#### 4.2.2 核心流程

`finish_par` 分四步：

1. **折叠空格**：`collapse_spaces(sink, start)` 在 `[start..]` 段上原地左移，丢掉首尾空格、合并相邻空格（见 4.2.3 与 spaces.rs）。
2. **取 span**：`select_span(elems)` 取第一个非 detached 的 span，作为新 `ParElem` 的来源定位。
3. **repack**：`(body, trunk) = repack(elems)`，得到重新打包的 content 树与公共样式链。
4. **建元素并回喂**：`grouped.end()` 截断 sink，构造 `ParElem::new(body)`，生命周期延长后 `visit(s, elem, trunk)`。

#### 4.2.3 源码精读

`finish_par` 全貌：

[crates/typst-realize/src/lib.rs:L1188-L1203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1188-L1203) —— `finish_par`：折叠空格 → 取 span → repack → `end()` 截断 → `visit` 新 `ParElem`。

逐行说明：

- `let (sink, start) = grouped.get_mut();` 借助 `Grouped::get_mut`（[lib.rs:L229-L231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L229-L231)）拿到 `(&mut sink, start)`，只对本分组这段做空格折叠。
- `collapse_spaces(sink, start)` 见 [spaces.rs:L30-L75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L30-L75)：单趟扫描、用 `copy_within` 原地左移，状态机 `SpaceState`（`Invisible/Destructive/Supportive/Space`，[spaces.rs:L11-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L11-L21)）决定每个空格留还是删。空格折叠的细节是 u2-l11 的主题，这里只需知道它在 repack 之前把段内的空格规整好。
- `let elems = grouped.get();` 取 `&sink[start..]`（折叠后的子元素，含残留的 `TagElem`）。
- `select_span(elems)`：[lib.rs:L1506-L1509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1506-L1509)，`Span::find` 取第一个非 detached 的 span。
- `let (body, trunk) = repack(elems);` —— 核心打包，下一节展开。
- `let s = grouped.end();`：[lib.rs:L235-L238](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L235-L238)，`truncate(start)` 把这段从 sink 删除并交还 `State`。
- `ParElem::new(body).pack().spanned(span)`：`ParElem` 由 `#[elem(...)]` 宏生成（`body: Content` 字段见 [model/par.rs:L98-L99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/par.rs#L98-L99) 与 L434），`new` 即宏生成的构造器。`s.store(...)` 把它放进 arena 延长生命周期，再 `visit(s, elem, trunk)` 重新进入流水线——新 `ParElem` 最终会走兜底 `push` 落进 sink。

**repack 详解**：

[crates/typst-realize/src/lib.rs:L1511-L1532](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1511-L1532) —— `repack`：算 trunk → 按相同样式链分段 → 每段提 suffix 重新打包。

逐步拆解：

1. `trunk = StyleChain::trunk_from_pairs(buf).unwrap_or_default()`：trunk 是所有 pair 样式链的**最长公共前缀**。注意 `trunk_from_pairs` 会**先过滤掉 `TagElem`** 再算（tag 不参与公共样式计算）：

   [crates/typst-library/src/foundations/styles.rs:L763-L765](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L763-L765) —— `trunk_from_pairs` 委托 `trunk`，跳过 `TagElem`。

   `trunk` 的算法（[styles.rs:L731-L743](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L731-L743)）是：以第一条链为基准，逐条比较，遇到更短的链就不断 `pop()` 基准的尾部，直到所有链在最浅深度处一致——这个公共深度就是 trunk。

2. `depth = trunk.links().count()`：trunk 由多少层样式「链接」组成。`links()` 见 [styles.rs:L702-L704](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L702-L704)。

3. `for (chain, group) in buf.group_by_key(|&(_, s)| s)`：把**连续的**、样式链 `s` 相同的 pair 聚成一段（`group_by_key` 是 `typst-utils::SliceExt` 提供的「连续分组」，语义同标准库 `slice::group_by`，[typst-utils/src/lib.rs:L131-L134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L131-L134)）。

4. 对每一段：`suffix = chain.suffix(depth)` 取该链在 trunk 之后的「局部样式」（[styles.rs:L715-L723](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L715-L723)：取 `links().count() - depth` 层并反转成 `Styles`）。然后分三种情况：

   - `suffix.is_empty()`：本段没有额外样式（就是纯 trunk），直接把元素原样塞进 `seq`。
   - 段里只有**一个**元素：`element.clone().styled_with_map(suffix)`，给单个元素套上局部样式。
   - 段里有**多个**元素：`Content::sequence(iter).styled_with_map(suffix)`，先串成序列再整体套样式（避免给每个子元素重复挂同一份 suffix）。

5. 返回 `(Content::sequence(seq), trunk)`：`Content::sequence`（[content/mod.rs:L239-L246](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L239-L246)）在子项为空时返回 `Content::empty()`、只有一个时直接返回它，否则才真正建序列。

用伪代码概括 repack 的本质：

```
trunk   = 公共样式前缀(所有 pair 的样式链)
depth   = trunk 的层数
body    = []
for 每一段「连续且样式链相同」的 pair:
    suffix = 这一段样式链去掉 trunk 后剩下的部分
    if suffix 为空:      body += 原样元素
    elif 段长 == 1:      body += 单元素.styled(suffix)
    else:                body += sequence(元素们).styled(suffix)
return (sequence(body), trunk)
```

最终 `finish_par` 把 trunk 作为「段落级样式」传给 `visit(ParElem, trunk)`，而每个子元素的局部差异样式则以 `StyledElem` 的形式内嵌在 `body` 里。这正是「摊平 → 重新打包」的逆过程。

#### 4.2.4 代码实践

**实践目标**：亲手观察 `repack` 如何对一段「多种行内样式混排」的段落做 trunk 提取与 suffix 打包。

**操作步骤**（需本地编译 typst）：

1. 在 `crates/typst-realize/src/lib.rs` 的 `finish_par` 里，`repack` 调用之后插入日志：

   ```rust
   let (body, trunk) = repack(elems);
   eprintln!("[finish_par] children={}, trunk={:?}",
       elems.len(), trunk);
   ```

2. 在 `repack` 里，算出 `trunk` 与每段 `suffix` 之后插入日志：

   ```rust
   let depth = trunk.links().count();
   eprintln!("[repack] pairs={}, trunk_links={}", buf.len(), depth);
   // ...进入循环后：
   let suffix = chain.suffix(depth);
   eprintln!("[repack] run_len={}, suffix_empty={}",
       group.len(), suffix.is_empty());
   ```

3. 准备一个混排行内样式的 Typst 文档 `doc.typ`（示例代码）：

   ```typst
   #set page(width: 240pt, height: auto)
   A _cat_ and a *dog* and a #highlight[fox] here.
   ```

4. 编译并捕获 stderr：

   ```bash
   cargo build --release -p typst
   ./target/release/typst compile doc.typ doc.pdf 2> realize.log
   ```

**需要观察的现象**：`realize.log` 里会出现多次 `[finish_par]` / `[repack]`（因为内省会多次重跑 realize）；定位到 `doc.typ` 正文那一段对应的记录，观察：
- `children` 数（pair 个数）远大于「样式段」个数。
- 每个 `run_len` 对应一段相同样式的连续文本。
- 带斜体/加粗/高亮的段 `suffix_empty=false`，纯文本段 `suffix_empty=true`。

**预期结果**（待本地验证）：`repack` 应当产出约 7~9 个 run（`"A "`、`"cat"`、`" and a "`、`"dog"`、`" and a "`、`"fox"`、`" here."`），其中 `"cat"`/`"dog"`/`"fox"` 三段 `suffix_empty=false`，其余为 `true`；最终 `body` 是一个 `Content::sequence`，trunk 只含页面级公共样式（如 `set page` 带来的那部分）。

> 说明：由于内省循环会多次调用 realize，日志会重复出现。这是正常现象，筛选出对应正文的记录即可。若不希望编译，可改为纯源码阅读：对照上面的伪代码，手工对示例文档跑一遍 `group_by_key` 与 `suffix` 的过程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `repack` 里要先算 trunk，而不是把每个 pair 的完整样式链都包回 `StyledElem`？
> **答案**：这样能把所有元素共享的公共样式「提到外面」交给 `visit(ParElem, trunk)`，避免在每个子元素上重复挂同一份样式，从而得到最小、最干净的 `body` 树。这也让下游排版能用一条样式链处理整段公共属性。

**练习 2**：`repack` 用 `group_by_key` 聚段，它的「连续」语义意味着什么？如果同一样式的两段文本中间夹着一段不同样式的文本，会聚成几段？
> **答案**：`group_by_key` 只合并**相邻且键相同**的元素。所以 `[A(斜), B(斜), C(粗), D(斜)]` 会聚成三段：`{A,B}(斜)`、`{C}(粗)`、`{D}(斜)`——`A` 与 `D` 虽然样式相同，但不相邻，不会合并。

**练习 3**：`finish_par` 调 `visit(s, elem, trunk)` 后，这个新 `ParElem` 会去哪里？
> **答案**：它会重新进入 `visit` 流水线。由于 `ParElem` 不命中 kind/show/序列/样式/分组/过滤任何分支，最终走兜底 `s.sink.push`（[lib.rs:L291](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L291)），成为 sink 里一个 well-known 元素，等待 layout 消费。

---

### 4.3 is_fully_inline_or_neutral：Fragment 的 inline 回退

#### 4.3.1 概念说明

`Fragment` 是为「行内容器」（如 box、某些表格单元格）准备的具现化场景。调用方传入 `kind: &mut FragmentKind`，初值通常是 `Block`。但如果这个 Fragment 里的内容**全部**是行内的（且没有段落断点），再给它套一层块级的 `ParElem` 就不对了——调用方期望拿到行内内容。

于是 realize 提供了一个「回退」：在最终收尾时，若发现「整个 Fragment 其实全是行内/中性内容」，就**不**生成 `ParElem`，而是把 `FragmentKind` 直接改写成 `Inline`，并把内容原样留在 sink 里（只做空格折叠）。这个判定函数就是 `is_fully_inline_or_neutral`。

#### 4.3.2 核心流程

判定为「全行内/中性」需要**同时**满足五个条件：

1. `kind` 是 `Fragment`。
2. 没有出现过段落断点（`!saw_parbreak`）。
3. 当前**恰好只有一个**活动分组，且它就是 `PAR`（`[grouping]` 单元素切片模式）。
4. sink 中该分组**之前**的部分，每个元素都是 `TagElem` 或对 PAR 呈 `Neutral`（即没有混入块级元素）。
5. （隐含）该 PAR 分组此时还没被收尾。

命中后，`finish()` 做三件事：把 `FragmentKind` 改成 `Inline`、`pop` 掉这个 PAR 分组（**不**调用 `finish_par`）、对整个 sink 做一次 `collapse_spaces`。

#### 4.3.3 源码精读

判定函数本体：

[crates/typst-realize/src/lib.rs:L1170-L1186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1170-L1186) —— `is_fully_inline_or_neutral`：用 `let` 链式条件一次性判定五个约束。

几个要点：

- `let [grouping] = s.groupings.as_slice()` 利用数组模式匹配，强制「恰好一个分组」；多了少了都不行。
- `std::ptr::eq(grouping.rule, &PAR)` 用指针相等确认它就是 PAR 规则本身（静态规则用指针身份比较）。
- `s.sink[..grouping.start]` 检查分组起点**之前**的内容——这部分只能是 tag 或中性元素，否则说明开头就有块级内容，不算「全行内」。
- 注意它**不检查**分组内部（`[grouping.start..]`）的元素类型：因为既然能形成唯一一个 PAR 分组且没被打断，内部按定义就是行内/中性内容。

它的唯一调用点在总收尾函数 `finish`：

[crates/typst-realize/src/lib.rs:L788-L810](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L810) —— `finish`：用 `finish_grouping_while` 驱动收尾，闭包里优先判断「全行内/中性」回退。

闭包逻辑：

- 命中 `is_fully_inline_or_neutral`：`**kind = FragmentKind::Inline`（`kind` 是 `&mut FragmentKind`，`**` 解引用到内层），`pop` 掉 PAR 分组，`collapse_spaces(&mut s.sink, 0)` 对整段做空格折叠，返回 `false` 让 `finish_grouping_while` **停止**收尾——于是 `finish_par` 根本不会被调用，段落被「放过」。
- 否则返回 `!s.groupings.is_empty()`，照常逐层 `finish_innermost_grouping`（PAR 会走到 `finish_par` 生成 `ParElem`）。

另外，`finish` 末尾还有一段：当 `kind` 是 `Par` 或 `Math` 时，空格是**顶层**的，需要单独 `collapse_spaces(&mut s.sink, 0)`（[lib.rs:L805-L807](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L805-L807)）——因为这两种 kind 用的是 `PAR_RULES`/`MATH_RULES`，根本不含 PAR 分组，空格折叠只能由 `finish` 兜底。

`is_fully_inline_or_neutral` 还有一个第二调用点：`finish_interrupted` 在样式中断分组时也会先问它一遍，命中就把 `groupings[0].interrupted` 置 `true` 并暂不收尾（[lib.rs:L819-L827](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L819-L827)）。这正是 `Grouping.interrupted` 字段注释里说的「may be ignored due to being fully inline」（[lib.rs:L152-L156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L152-L156)）——把「是否真的要收尾段落」这个决定推迟到最后的 `finish`。

#### 4.3.4 代码实践

**实践目标**：验证「全行内 Fragment 会改写成 Inline、跳过 `finish_par`」。

**操作步骤**（源码阅读型）：

1. 在 `finish` 的闭包里、`is_fully_inline_or_neutral(s)` 命中分支插入日志：

   ```rust
   if is_fully_inline_or_neutral(s) {
       eprintln!("[finish] downgrade Fragment -> Inline, skip finish_par");
       if let RealizationKind::Fragment { kind } = &mut s.kind {
           **kind = FragmentKind::Inline;
       }
       // ...
   ```

2. 同时在 `finish_par` 开头插入 `eprintln!("[finish_par] called");`。

3. 用两个文档对比（示例代码）：

   ```typst
   % inline.typ —— 全行内
   #box[A *bold* cat]
   ```

   ```typst
   % block.typ —— 含块级内容
   #box[Before

   After]
   ```

   （第二个文档里 `#box[]` 内出现空行，构成段落断点。）

4. 分别编译，对比 `realize.log`。

**需要观察的现象**：
- `inline.typ`：出现 `[finish] downgrade Fragment -> Inline`，且**没有** `[finish_par] called`。
- `block.typ`：因 `saw_parbreak` 为真，`is_fully_inline_or_neutral` 不命中，会走正常收尾、出现 `[finish_par] called`。

**预期结果**（待本地验证）：全行内的 box 内容不会被打包成 `ParElem`，`FragmentKind` 被改写为 `Inline`；含段落断点的内容则正常生成 `ParElem`。

#### 4.3.5 小练习与答案

**练习 1**：为什么条件里要有 `!s.saw_parbreak`？
> **答案**：段落断点（空行）是用户明确表达「这里要分段」的信号。一旦见过 parbreak，哪怕内容都是行内的，也说明 Fragment 里有多段，应当生成 `ParElem`，不能当作单一行内流回退。所以 `saw_parbreak` 直接取消回退资格。

**练习 2**：如果 Fragment 里只有一个块级元素（比如一个标题），`is_fully_inline_or_neutral` 会命中吗？
> **答案**：不会。块级元素对 PAR 是 `Interrupt`，它会打断 PAR 分组；最终 `s.groupings` 不会是「恰好一个 PAR 分组」，要么没有 PAR 分组、要么栈里有别的结构，`let [grouping] = ...` 与 `ptr::eq(rule, &PAR)` 无法同时满足，函数返回 `false`，照常生成段落。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「段落诞生全链路」追踪。

**任务**：用下面这个文档（示例代码）作为输入，完整解释一段混合样式文本是如何变成 `ParElem` 的：

```typst
#set page(width: 240pt)
#set align(center)
Hello *world* and #highlight[Typst].
```

**要求你讲清下面这条链路上的每一跳**：

1. **播种**：`TextElem("Hello ")` 为何先进 TEXTUAL 分组而非 PAR？`finish_textual` 在哪里、用什么条件把它移交/新建为 PAR 分组？（参考 4.1.3）
2. **收尾触发**：行末的 `.` 之后没有更多行内元素时，PAR 分组在 `finish` 里如何被收尾？（参考 u2-l7 与 4.2.2）
3. **折叠**：`finish_par` 调 `collapse_spaces(sink, start)` 时，段首/段尾的空格会如何被处理？（参考 4.2.3、spaces.rs）
4. **打包**：对 `"Hello "`(plain)、`"world"`(strong)、`" and "`(plain)、`"Typst"`(highlight)、`"."`(plain) 这几段，`repack` 算出的 trunk 是什么？哪些段 `suffix_empty=true`、哪些为 `false`？多元素同样式段会走 `Content::sequence(...).styled_with_map(suffix)` 还是单元素分支？（参考 4.2.3）
5. **回喂**：`ParElem::new(body)` 经 `s.store` 与 `visit(s, elem, trunk)` 后，最终落到 sink 的哪一行？（参考 4.2.3）

**交付物**：一张标注了 trunk、各 suffix、最终 `body` 树结构的示意图（手绘即可），并能指出每一步对应的源码行号。

> 提示：第 4 步里 `#set align(center)` 是页面级样式，会进入 trunk；`*world*` 的 strong 与 `#highlight[...]` 的填充色则是各自段的 suffix。具体取值待本地验证。

## 6. 本讲小结

- `PAR` 规则（priority 1，最低）把 `TextElem/HElem/Linebreak/SmartQuote/Inline/Box` 判为 `Trigger`、`SpaceElem` 为 `Inner`、部分 `HtmlElem` 为 `Neutral`、其余块级元素为 `Interrupt`；它是分组栈最外层的段落容器。
- 段落常常不是 `visit_grouping_rules` 直接启动的，而是文本先进 TEXTUAL（priority 3）、由 `finish_textual` 在无正则命中时**播种**出一个 PAR 分组。
- `finish_par` 四步：`collapse_spaces` 折叠空格 → `select_span` 取 span → `repack` 打包 → `end()` 截断后 `visit(ParElem, trunk)`。
- `repack` 是「拍平」的逆过程：用 `trunk_from_pairs` 算公共样式前缀 trunk，按 `group_by_key` 把连续同样式 pair 分段，每段用 `suffix(depth)` 提取局部样式并以 `StyledElem` 包回，最后返回 `(Content::sequence(seq), trunk)`。
- `is_fully_inline_or_neutral` 在「Fragment + 无 parbreak + 唯一 PAR 分组 + 前置全是 tag/neutral」时命中，使 `finish` 放弃生成 `ParElem`、把 `FragmentKind` 改写成 `Inline`，这是行内容器的关键回退路径。

## 7. 下一步学习建议

- **u2-l9（列表/枚举/术语与引用分组）**：去看 priority 2 的 `LIST/ENUM/TERMS` 与 `CITES` 如何复用 `list_like_grouping`，它们的 `finish_list_like` 同样依赖 trunk/suffix 提取，是本讲 `repack` 思路的姐妹实现。
- **u2-l10（文本分组与正则 show 规则）**：深入 `finish_textual` 的另一条分支——`visit_textual`/`find_regex_match_in_elems` 如何跨多个文本元素做正则匹配，理解 PAR 与 TEXTUAL 协作的全貌。
- **u2-l11（空格折叠 spaces.rs）**：精读 `collapse_spaces` 的原地左移算法与 `SpaceState` 四态机，补全本讲只点到为止的空格处理细节。
- 若你对「trunk/suffix 为何这样设计」还意犹未尽，可直接对照阅读 `crates/typst-library/src/foundations/styles.rs` 里 `StyleChain::trunk` / `suffix` / `links` 的实现。
